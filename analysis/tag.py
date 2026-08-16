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
DEFAULT_TAGGER = "anthropic/claude-haiku-4.5"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 1024
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
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return [], None, None, f"not JSON: {text[:120]!r}"
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return [], None, None, f"not JSON: {text[:120]!r}"
    if not isinstance(data, dict):
        return [], None, None, f"not an object: {text[:120]!r}"

    valid = set(book.code_names)
    codes = [c for c in (data.get("codes") or []) if isinstance(c, str) and c in valid]
    if not codes:
        return [], None, None, f"no valid codes in {data.get('codes')!r}"
    quote = data.get("flagged_quote")
    quote = quote.strip() if isinstance(quote, str) and quote.strip() else None
    notable = data.get("notable") if isinstance(data.get("notable"), bool) else None
    return sorted(set(codes)), quote, notable, None


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
