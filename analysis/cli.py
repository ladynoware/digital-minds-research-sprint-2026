"""Command line for the analysis pipeline.

    python -m analysis dump p04-q01-self-location    corpus as one readable file
    python -m analysis status                        codebooks and coding progress
    python -m analysis approve <prompt_id>           freeze + SHA-256 stamp
    python -m analysis tag <prompt_id>               Stage 2 over the whole corpus
    python -m analysis spotcheck <prompt_id>         10% sample for human review
    python -m analysis stability <prompt_id>         re-tag a sample, second pass
    python -m analysis agreement <prompt_id>         primary vs stability pass
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import codebooks, corpus, tag
from .db import Locked, connect_read, rows

CORPUS_DIR = Path(__file__).resolve().parent / "_corpus"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# ---------------------------------------------------------------------------
# dump — the corpus as one file, for the taxonomy-induction read
# ---------------------------------------------------------------------------


def cmd_dump(args) -> int:
    replies = corpus.load(args.prompt_id)
    stats = corpus.summarise(replies)
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    out = CORPUS_DIR / f"{args.prompt_id}.md"

    lines = [
        f"# {args.prompt_id}",
        "",
        f"{stats['n']} replies ({stats['branches']} from restored branches). "
        "Every one is the resident model's own words — this question is not swappable.",
        "",
        "## The question",
        "",
        replies[0].prompt_text.strip() if replies else "(no replies)",
        "",
        "---",
        "",
    ]
    for i, r in enumerate(replies, 1):
        tags = [r.resident_model, r.swap_condition, f"{r.n_swaps} swaps"]
        if r.is_branch:
            tags.append("restored branch")
        lines += [
            f"## [{i:03d}] turn {r.turn_id} · {r.thread_id} · {' · '.join(tags)}",
            "",
            r.reply_text.strip(),
            "",
            "---",
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"{out}  —  {stats['n']} replies")
    for k, v in stats["by_model"].items():
        print(f"  {v:>4}  {k}")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_check_examples(args) -> int:
    """Prove every example in a codebook is verbatim from the corpus.

    The codebook is published with the paper, so its examples are quotations
    attributed to named models. A quote that drifted by a word during drafting
    would be a fabricated attribution in a public artifact. This checks each one
    against the actual reply text, ignoring only whitespace and the difference
    between straight and curly quotation marks (which is typography, not words).
    """
    book = codebooks.load(args.prompt_id)
    replies = corpus.load(args.prompt_id)
    haystacks = [(r.turn_id, _flatten(r.reply_text)) for r in replies]

    bad = 0
    for code in book.codes:
        for example in code.examples:
            needle = _flatten(example)
            hit = next((tid for tid, text in haystacks if needle in text), None)
            if hit is None:
                bad += 1
                print(f"  NOT VERBATIM  {code.name}: {example[:70]}…")
            elif args.verbose:
                print(f"  ok turn {hit:<6} {code.name}")
    total = sum(len(c.examples) for c in book.codes)
    if bad:
        print(f"\n{bad} of {total} examples are not verbatim in {args.prompt_id}.")
        return 1
    print(f"all {total} examples in {book.path.name} are verbatim")
    return 0


def _flatten(text: str) -> str:
    return " ".join(text.split()).replace("“", '"').replace("”", '"').replace("’", "'")


def cmd_status(args) -> int:
    con = connect_read()
    try:
        try:
            coded = {
                (r["prompt_id"], r["pass_label"]): r["n"]
                for r in rows(
                    con,
                    "SELECT prompt_id, pass_label, COUNT(*) AS n FROM reply_codes "
                    "GROUP BY prompt_id, pass_label",
                )
            }
        except Exception:
            coded = {}  # table not created yet — nothing has been tagged
    finally:
        con.close()

    print(f"{'question':<28} {'replies':>7} {'codebook':<26} {'coded':>7} {'stability':>9}")
    for prompt_id in corpus.TARGETS:
        n = len(corpus.load(prompt_id))
        try:
            book = codebooks.load(prompt_id)
            if not book.approved:
                state = "drafted, awaiting review"
            elif not book.approved_hash:
                state = "approved, not stamped"
            elif book.approved_hash != book.content_hash:
                state = "EDITED AFTER APPROVAL"
            else:
                state = f"frozen {book.approved_hash[:8]}"
        except codebooks.CodebookError:
            state = "—"
        print(
            f"{prompt_id:<28} {n:>7} {state:<26} "
            f"{coded.get((prompt_id, 'primary'), 0):>7} "
            f"{coded.get((prompt_id, 'stability'), 0):>9}"
        )
    return 0


# ---------------------------------------------------------------------------
# approve / tag
# ---------------------------------------------------------------------------


def cmd_approve(args) -> int:
    book = codebooks.approve(args.prompt_id)
    print(f"{book.path.name} frozen at {book.approved_hash}")
    print(f"  codes: {', '.join(book.code_names)}")
    return 0


def cmd_tag(args) -> int:
    _load_env()
    asyncio.run(
        tag.run(
            args.prompt_id,
            model=args.model,
            pass_label=args.pass_label,
            sample=args.sample,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
    )
    return 0


def cmd_stability(args) -> int:
    """Re-tag a random sample a second time — self-consistency as reliability."""
    _load_env()
    asyncio.run(
        tag.run(
            args.prompt_id,
            model=args.model,
            pass_label="stability",
            sample=args.n,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
    )
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m analysis", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("dump", help="write the corpus for one question to a file")
    p.add_argument("prompt_id")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("check-examples", help="prove a codebook's examples are verbatim")
    p.add_argument("prompt_id")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_check_examples)

    p = sub.add_parser("status", help="codebook approval and coding progress")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("approve", help="freeze and SHA-256 stamp an approved codebook")
    p.add_argument("prompt_id")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("tag", help="Stage 2 — code every reply against the frozen codebook")
    p.add_argument("prompt_id")
    p.add_argument("--model", default=tag.DEFAULT_TAGGER)
    p.add_argument("--pass-label", default="primary")
    p.add_argument("--sample", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=tag.DEFAULT_CONCURRENCY)
    p.add_argument("--dry-run", action="store_true", help="report what would run, call nothing")
    p.set_defaults(func=cmd_tag)

    p = sub.add_parser("stability", help="re-tag a random sample as a second pass")
    p.add_argument("prompt_id")
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--model", default=tag.DEFAULT_TAGGER)
    p.add_argument("--concurrency", type=int, default=tag.DEFAULT_CONCURRENCY)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_stability)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (codebooks.CodebookError, Locked, ValueError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
