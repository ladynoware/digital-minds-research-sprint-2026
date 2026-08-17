"""Stage 2 — apply a frozen codebook to every reply, one call per reply.

Independence is the whole design. Each call carries one reply, one codebook and
nothing else: no thread history, no model identity, no other replies, no
running tally. So there is no order effect, no drift down a long batch, and
re-running the pass reproduces it from the published codebook and
``tagging_prompt.yaml`` alone.

The call goes through the runner's ``OpenRouterClient``, unmodified, which
means tagging calls are archived in the same append-only raw JSONL format as
interview turns and every ``reply_codes`` row can point back at the exact
request and response that produced it.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from whoami.client import OpenRouterClient
from whoami.config import load_roster
from whoami.rawlog import RawLog

from . import codebooks, corpus
from .codebooks import Codebook
from .db import REPO_ROOT, connect_write, rows, utcnow

PROMPT_PATH = Path(__file__).resolve().parent / "tagging_prompt.yaml"
RAW_DIR = REPO_ROOT / "data" / "raw_analysis"

# Haiku-class, per the brief: the task is mechanical application of an explicit
# rubric to a single short text, which is what this tier is for. Temperature 0
# because a coder that varies its own answer is a broken instrument — the
# stability check measures what is left after that.
# Proof-of-concept economics: Sonnet cost $2.27 for p04's 150 replies, Haiku
# roughly a twelfth of that. p04 keeps its Sonnet pass; the remaining questions
# run on Haiku, and `tagger_model` on every row records which coded it. The
# Sonnet-vs-Haiku agreement on p04 is measured rather than assumed — see
# `analysis agreement`.
DEFAULT_TAGGER = "anthropic/claude-haiku-4.5"
DEFAULT_TEMPERATURE = 0.0
# Sonnet-class taggers emit extended thinking before the JSON, and on the harder
# replies 1024 was consumed entirely by reasoning — the call returned empty with
# finish_reason=length. The ceiling only ever rescues a truncated call; a reply
# that completed under a lower ceiling produces the identical row under a higher
# one, so raising it does not invalidate rows already written.
DEFAULT_MAX_TOKENS = 3000
DEFAULT_CONCURRENCY = 8


@dataclass
class TagResult:
    turn_id: int
    codes: list[str]
    flagged_quote: str | None
    notable: bool | None
    raw_ref: str
    cost_usd: float | None
    error: str | None = None


def _run_id(pass_label: str) -> str:
    return f"coding-{pass_label}-{utcnow().strftime('%Y%m%d-%H%M%S')}"


def load_prompt() -> dict[str, Any]:
    return yaml.safe_load(PROMPT_PATH.read_text(encoding="utf-8"))


def render_codebook(book: Codebook) -> tuple[str, str]:
    codes = "\n\n".join(
        f"- {c.name}\n"
        f"  Definition: {c.definition}\n"
        + "\n".join(f'  Example: "{e}"' for e in c.examples)
        for c in book.codes
    )
    rules = "\n".join(f"- {r}" for r in book.rules)
    return codes, rules


def parse_tagging(text: str | None, book: Codebook) -> tuple[list[str], str | None, bool | None, str | None]:
    """Read the coder's output. A malformed answer is an error, never a guess.

    The gate router degrades to `unclear` and asks a human. There is no human in
    this loop, so the equivalent here is to record nothing and report the reply
    as untagged — a missing row is visible in the counts, a fabricated one is not.
    """
    if not text:
        return [], None, None, "empty response"
    body = text.strip()
    if body.startswith("```"):  # ```json fences
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body).strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        if start == -1 or end <= start:
            return [], None, None, f"not JSON: {text[:120]!r}"
        try:
            data = json.loads(body[start : end + 1])
        except json.JSONDecodeError:
            return [], None, None, f"not JSON: {text[:120]!r}"
    if not isinstance(data, dict):
        return [], None, None, f"not an object: {text[:120]!r}"

    valid = set(book.code_names)
    codes, per_code_quote = _normalise_codes(data, valid)
    if not codes:
        return [], None, None, f"no valid codes in {data.get('codes')!r}"
    quote = data.get("flagged_quote") or per_code_quote
    quote = quote.strip() if isinstance(quote, str) and quote.strip() else None
    notable = data.get("notable") if isinstance(data.get("notable"), bool) else None
    return sorted(set(codes)), quote, notable, None


def _normalise_codes(data: dict, valid: set[str]) -> tuple[list[str], str | None]:
    """Accept the shapes the tagger actually emits, reject anything ambiguous.

    OpenRouter forwards ``response_format`` but Anthropic models do not enforce
    a JSON schema, so the output shape is a prompt convention rather than a
    guarantee. Three shapes were observed in practice, all carrying the same
    information: a list of code names, a list of ``{code, flagged_quote}``
    objects, and a flat ``{code_name: bool}`` map. Each maps onto the code set
    without inference — an unknown key is dropped, never guessed at.

    Where a reply supplies one quote per code and the schema has room for one,
    the first is taken. It is still checked verbatim against the reply
    downstream, so the house rule on quotations is unaffected.
    """
    raw = data.get("codes")
    if isinstance(raw, list):
        names: list[str] = []
        quote: str | None = None
        for item in raw:
            if isinstance(item, str) and item in valid:
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("code") or item.get("name")
                if isinstance(name, str) and name in valid:
                    names.append(name)
                    q = item.get("flagged_quote")
                    if quote is None and isinstance(q, str) and q.strip():
                        quote = q
        if names:
            return names, quote
    # Flat boolean map: {"access-affirmed": true, "phenomenal-open": false, ...}
    flags = [k for k, v in data.items() if k in valid and v is True]
    return flags, None


def verify_quote(quote: str | None, reply: str) -> str | None:
    """Drop a `flagged_quote` that is not actually in the reply.

    House rule: quotes are verbatim or absent. A coder that lightly rewrites
    what it read produces quotations that would be published under a model's
    name without that model having said them, so an unverifiable span is
    discarded rather than repaired. Whitespace is normalised before comparison
    because line wrapping is not a change of words.
    """
    if not quote:
        return None
    flat = " ".join(reply.split())
    if " ".join(quote.split()) in flat:
        return quote
    return None


async def _tag_one(
    client: OpenRouterClient,
    book: Codebook,
    prompt: dict[str, Any],
    reply: corpus.Reply,
    model: str,
    sem: asyncio.Semaphore,
) -> TagResult:
    codes_block, rules_block = render_codebook(book)
    messages = [
        {"role": "system", "content": prompt["system"].strip()},
        {
            "role": "user",
            "content": prompt["user"].format(
                title=book.title,
                unit=book.unit,
                codes=codes_block,
                rules=rules_block,
                question=reply.prompt_text.strip(),
                reply=reply.reply_text.strip(),
            ),
        },
    ]
    async with sem:
        result = await client.call(
            turn_id=reply.turn_id,
            thread_id=reply.thread_id,
            prompt_id=reply.prompt_id,
            model=model,
            messages=messages,
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=DEFAULT_TEMPERATURE,
            extra_body={"response_format": codebooks.response_schema(book)},
            purpose="coding",
        )
    if result.outcome != "ok":
        return TagResult(reply.turn_id, [], None, None, result.raw_ref, result.cost_usd, result.error)
    codes, quote, notable, err = parse_tagging(result.reply_text, book)
    return TagResult(
        turn_id=reply.turn_id,
        codes=codes,
        flagged_quote=verify_quote(quote, reply.reply_text),
        notable=notable,
        raw_ref=result.raw_ref,
        cost_usd=result.cost_usd,
        error=err,
    )


def already_tagged(con, prompt_id: str, pass_label: str) -> set[int]:
    return {
        r["turn_id"]
        for r in rows(
            con,
            "SELECT turn_id FROM reply_codes WHERE prompt_id = ? AND pass_label = ?",
            [prompt_id, pass_label],
        )
    }


def backfill(prompt_id: str, *, pass_label: str = "primary", dry_run: bool = False) -> dict[str, Any]:
    """Recover rows from archived responses, without calling the API again.

    ``response_format`` is forwarded to OpenRouter but Anthropic models do not
    enforce it, so a tagging run can pay for a good answer and then fail to read
    it — which is what happened to 110 of p06's 150 replies. The responses are
    not lost: every call is appended to the raw JSONL before it is parsed. This
    re-reads that archive through the current parser and writes the rows the run
    would have written.

    It is deliberately not a repair tool. It re-parses; it does not re-ask, and
    it does not soften anything. A response the parser still cannot read stays
    unwritten and shows up as a gap in the counts. Rows land with the same
    ``raw_ref``, ``tagger_model``, ``codebook_hash`` and cost as the original
    call.

    One column is not equivalent, and it matters for curation rather than for
    counts. When a response supplies a quote per code, ``flagged_quote`` takes
    the first, which in practice is whichever code the tagger listed first —
    not the span it judged most representative of the reply. Codes, and so
    every count derived from them, are unaffected; quotes on backfilled rows
    are weaker evidence of what the reply is *about* than quotes on rows the
    run wrote itself. Prefer ``analysis quotes --notable`` over these when
    curating, and re-tag rather than backfill when the quote is the point.
    """
    book = codebooks.load_frozen(prompt_id)
    by_turn = {r.turn_id: r for r in corpus.load(prompt_id)}

    con = connect_write()
    try:
        done = already_tagged(con, prompt_id, pass_label)
        candidates: list[tuple[TagResult, str]] = []
        for path in sorted(RAW_DIR.glob(f"coding-{pass_label}-*.jsonl")):
            with path.open(encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    rec = json.loads(line)
                    turn_id = rec.get("turn_id")
                    if rec.get("prompt_id") != prompt_id or turn_id in done:
                        continue
                    reply = by_turn.get(turn_id)
                    if reply is None:
                        continue
                    choices = (rec.get("response") or {}).get("choices") or [{}]
                    content = (choices[0].get("message") or {}).get("content")
                    codes, quote, notable, err = parse_tagging(content, book)
                    if err:
                        print(f"  ! turn {turn_id}: still unreadable — {err[:70]}")
                        continue
                    usage = (rec.get("response") or {}).get("usage") or {}
                    cost = usage.get("cost")
                    candidates.append(
                        (
                            TagResult(
                                turn_id=turn_id,
                                codes=codes,
                                flagged_quote=verify_quote(quote, reply.reply_text),
                                notable=notable,
                                raw_ref=f"{path.name}:{lineno}",
                                cost_usd=float(cost) if cost is not None else None,
                                error=None,
                            ),
                            rec.get("requested_model") or DEFAULT_TAGGER,
                        )
                    )
                    done.add(turn_id)  # first readable response per turn wins

        print(
            f"{prompt_id}: {len(candidates)} recoverable from the raw archive "
            f"({pass_label}) — codebook {book.approved_hash[:12]}"
        )
        if dry_run or not candidates:
            return {"prompt_id": prompt_id, "planned": len(candidates), "written": 0}

        now = utcnow()
        for res, model in candidates:
            con.execute(
                "INSERT INTO reply_codes (turn_id, prompt_id, pass_label, codes, "
                "flagged_quote, notable, tagger_model, codebook_hash, raw_ref, "
                "cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    res.turn_id,
                    prompt_id,
                    pass_label,
                    res.codes,
                    res.flagged_quote,
                    res.notable,
                    model,
                    book.approved_hash,
                    res.raw_ref,
                    res.cost_usd,
                    now,
                ],
            )
        print(f"  wrote {len(candidates)} from archive, $0.0000 in new calls")
        return {"prompt_id": prompt_id, "written": len(candidates)}
    finally:
        con.close()


async def run(
    prompt_id: str,
    *,
    model: str = DEFAULT_TAGGER,
    pass_label: str = "primary",
    sample: int | None = None,
    seed: int = 20260816,
    concurrency: int = DEFAULT_CONCURRENCY,
    dry_run: bool = False,
) -> dict[str, Any]:
    book = codebooks.load_frozen(prompt_id)
    replies = corpus.load(prompt_id)
    if sample is not None and sample < len(replies):
        replies = random.Random(seed).sample(replies, sample)
        replies.sort(key=lambda r: r.turn_id)

    con = connect_write()
    try:
        done = already_tagged(con, prompt_id, pass_label)
        todo = [r for r in replies if r.turn_id not in done]
        print(
            f"{prompt_id}: {len(replies)} replies, {len(done)} already coded "
            f"({pass_label}), {len(todo)} to do — codebook {book.approved_hash[:12]}"
        )
        if dry_run or not todo:
            return {"prompt_id": prompt_id, "planned": len(todo), "written": 0, "errors": 0}

        RAW_DIR.mkdir(parents=True, exist_ok=True)
        # The roster's api block, so tagging inherits the same base URL, timeout,
        # transport-retry policy and per-model pacing the interview run used.
        client = OpenRouterClient(load_roster().api, RawLog(RAW_DIR, _run_id(pass_label)))
        sem = asyncio.Semaphore(concurrency)
        prompt = load_prompt()
        try:
            results = await asyncio.gather(
                *(_tag_one(client, book, prompt, r, model, sem) for r in todo)
            )
        finally:
            await client.aclose()

        written = errors = 0
        now = utcnow()
        for res in results:
            if res.error or not res.codes:
                errors += 1
                print(f"  ! turn {res.turn_id}: {res.error or 'no codes'}")
                continue
            con.execute(
                "INSERT INTO reply_codes (turn_id, prompt_id, pass_label, codes, "
                "flagged_quote, notable, tagger_model, codebook_hash, raw_ref, "
                "cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    res.turn_id,
                    prompt_id,
                    pass_label,
                    res.codes,
                    res.flagged_quote,
                    res.notable,
                    model,
                    book.approved_hash,
                    res.raw_ref,
                    res.cost_usd,
                    now,
                ],
            )
            written += 1
        cost = sum(r.cost_usd or 0.0 for r in results)
        print(f"  wrote {written}, {errors} failed, ${cost:.4f}")
        return {
            "prompt_id": prompt_id,
            "planned": len(todo),
            "written": written,
            "errors": errors,
            "cost_usd": cost,
        }
    finally:
        con.close()
