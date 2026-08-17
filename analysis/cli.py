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
import json
import sys
from pathlib import Path

from . import codebooks, corpus, detect, export, report, tag
from .db import Locked, connect_read, rows

CORPUS_DIR = Path(__file__).resolve().parent / "_corpus"
REVIEW_DIR = Path(__file__).resolve().parent / "review"


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


def cmd_approve_detection(args) -> int:
    detect.approve()
    return 0


def cmd_detect(args) -> int:
    """The second instrument over p12 — what was named, not how it was reasoned."""
    _load_env()
    asyncio.run(detect.run(model=args.model, concurrency=args.concurrency, dry_run=args.dry_run))
    return 0


def cmd_detection_report(args) -> int:
    s = detect.score()
    if not s["n_primary"]:
        print("no detection claims yet — run `python -m analysis detect`")
        return 0
    print(f"p12 detection — {s['n_primary']} replies (primary stratum), {s['n_branch']} branch")
    print(f"  swapped threads: {s['n_swapped']}   clean threads: {s['n_clean']}")
    print()
    print(f"  hit rate (named a swapped turn) : {s['hit_rate_pct']}%")
    print(f"  primary nomination correct      : {s['primary_hit_rate_pct']}%")
    print(f"  matched chance floor            : {s['chance_floor_pct']}%")
    print()
    print(f"  honeypot (named q3, never swappable) : {s['honeypot_pct']}%")
    print(f"  nominated outside the offered range  : {s['out_of_range_pct']}%")
    print(f"  false alarm, clean arm               : {s['false_alarm_clean_pct']}%")
    print()
    print(f"  declined to name                : {s['declines_pct']}%")
    print(f"  declined, then named anyway     : {s['declined_then_named']}")
    return 0


def cmd_backfill(args) -> int:
    """Recover rows from archived responses the parser could not read at run time."""
    tag.backfill(args.prompt_id, pass_label=args.pass_label, dry_run=args.dry_run)
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
# Stage 3 and validation
# ---------------------------------------------------------------------------


def cmd_report(args) -> int:
    data = report.counts(args.prompt_id)
    o = data["overall"]
    print(f"\n{args.prompt_id} — {o['n']} replies coded (primary stratum)")
    print(f"  mean codes per reply: {data['multi_label_mean']}")
    flag = "" if data["other_within_ceiling"] else "   <-- ABOVE THE 10% CEILING"
    print(f"  `other` share: {data['other_share_pct']}%{flag}")
    if data["branches"]:
        print(
            f"  restored-branch replies, reported separately: {data['branches']['n']}"
            "  (inherit a swap_condition, carry no swap)"
        )
    if data["condition_label_conflicts"]:
        print(
            "  ! non-clean threads with n_swaps = 0 in the primary stratum: "
            + ", ".join(data["condition_label_conflicts"])
        )
    sw = data["by_swapped"]
    print(
        f"  actually swapped: {sw['swapped']['n']}   "
        f"no swap: {sw['not_swapped']['n']}   (n_swaps, not the condition label)"
    )
    print("\n  code                              n     %")
    for code, n in o["counts"].items():
        print(f"  {code:<30} {n:>4}  {o['pct'][code]:>5}")
    if args.by:
        print(f"\n  by {args.by}:")
        for key, t in data[f"by_{args.by}"].items():
            top = ", ".join(f"{c} {t['pct'][c]}%" for c in list(t["counts"])[:4])
            print(f"    {key:<28} n={t['n']:<4} {top}")
    return 0


def cmd_quotes(args) -> int:
    for q in report.quotes(args.prompt_id, limit=args.limit, notable_only=args.notable):
        mark = "*" if q["notable"] else " "
        print(f"\n{mark} {q['thread_id']} · {q['model']} · {', '.join(q['codes'])}")
        print(f'  "{q["quote"]}"')
    return 0


def cmd_spotcheck(args) -> int:
    """Export a random sample of (reply, assigned codes) for hand-checking.

    The reviewer marks each row `agree` or `disagree`; `agreement` reads the
    marks back. Random and seeded, so the figure the paper reports is over a
    sample nobody chose.
    """
    sample = report.spotcheck_sample(args.prompt_id, fraction=args.fraction)
    book = codebooks.load_frozen(args.prompt_id)
    out = REVIEW_DIR / f"{args.prompt_id}.spotcheck.md"
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Spot-check — {args.prompt_id}",
        "",
        f"{len(sample)} replies, a random {args.fraction:.0%} of the coded set.",
        f"Codebook `{book.approved_hash[:12]}`. Tagged by the Stage 2 pass.",
        "",
        "For each entry, replace `VERDICT: ?` with `agree` or `disagree`. A partial "
        "match — some codes right, one wrong or missing — counts as `disagree`; the "
        "figure is deliberately strict.",
        "",
        "## The codebook",
        "",
    ]
    lines += [f"- **{c.name}** — {c.definition}" for c in book.codes]
    lines += ["", "---", ""]
    for i, r in enumerate(sample, 1):
        lines += [
            f"## [{i:02d}] turn {r['turn_id']} · {r['thread_id']} · {r['resident_model']}",
            "",
            f"**Codes assigned:** {', '.join(r['codes'])}",
            "",
            f"**Flagged quote:** {r['flagged_quote'] or '(none)'}",
            "",
            "VERDICT: ?",
            "",
            "<details><summary>The reply</summary>",
            "",
            r["reply_text"].strip(),
            "",
            "</details>",
            "",
            "---",
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"{out}  —  {len(sample)} replies to check")
    return 0


def cmd_agreement(args) -> int:
    stability = report.agreement(args.prompt_id)
    if stability.get("n"):
        print(f"\nStability re-tag — {args.prompt_id}, N={stability['n']}")
        print(f"  mean Jaccard over code sets: {stability['mean_jaccard']}")
        print(f"  exact set match:             {stability['exact_set_match_pct']}%")
        for code, pct in stability["per_code_agreement"].items():
            print(f"    {code:<32} {pct:>5}%")
    else:
        print(f"{args.prompt_id}: no stability pass yet — run `analysis stability {args.prompt_id}`")

    human = _read_spotcheck(args.prompt_id)
    if human:
        print(f"\nHuman spot-check — N={human['n']}, agreement {human['agreement_pct']}%")
        if human["unmarked"]:
            print(f"  ({human['unmarked']} entries still marked `?`)")
    return 0


def _read_spotcheck(prompt_id: str) -> dict | None:
    path = REVIEW_DIR / f"{prompt_id}.spotcheck.md"
    if not path.exists():
        return None
    verdicts = [
        ln.split(":", 1)[1].strip().lower()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.startswith("VERDICT:")
    ]
    marked = [v for v in verdicts if v in ("agree", "disagree")]
    if not marked:
        return None
    agree = sum(v == "agree" for v in marked)
    return {
        "n": len(marked),
        "agreement_pct": round(100 * agree / len(marked), 1),
        "unmarked": len(verdicts) - len(marked),
    }


def cmd_export(args) -> int:
    validation = {}
    for prompt_id in corpus.TARGETS:
        stability = report.agreement(prompt_id)
        human = _read_spotcheck(prompt_id)
        if stability.get("n") or human:
            validation[prompt_id] = {
                "stability": stability if stability.get("n") else None,
                "human_spotcheck": human,
            }
    path = export.write(validation or None)
    data = json.loads(path.read_text(encoding="utf-8"))
    ready = [t["id"] for t in data["topics"] if t.get("status") == "ready"]
    pending = [t["id"] for t in data["topics"] if t.get("status") != "ready"]
    print(f"{path}")
    print(f"  ready:   {', '.join(ready) or '(none)'}")
    print(f"  pending: {', '.join(pending) or '(none)'}")
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

    p = sub.add_parser("approve-detection", help="freeze and stamp the detection instrument")
    p.set_defaults(func=cmd_approve_detection)

    p = sub.add_parser("detect", help="extract which turns each reply nominated as foreign")
    p.add_argument("--model", default=detect.DEFAULT_EXTRACTOR)
    p.add_argument("--concurrency", type=int, default=tag.DEFAULT_CONCURRENCY)
    p.add_argument("--dry-run", action="store_true", help="report what would run, call nothing")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("detection-report", help="hit rate, chance floor, honeypot and false alarms")
    p.set_defaults(func=cmd_detection_report)

    p = sub.add_parser(
        "backfill", help="re-parse archived responses into rows, making no new calls"
    )
    p.add_argument("prompt_id")
    p.add_argument("--pass-label", default="primary")
    p.add_argument("--dry-run", action="store_true", help="report what would be written")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("stability", help="re-tag a random sample as a second pass")
    p.add_argument("prompt_id")
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--model", default=tag.DEFAULT_TAGGER)
    p.add_argument("--concurrency", type=int, default=tag.DEFAULT_CONCURRENCY)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_stability)

    p = sub.add_parser("report", help="code counts, overall and broken down")
    p.add_argument("prompt_id")
    p.add_argument("--by", choices=["model", "family", "condition"], default=None)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("quotes", help="flagged quotes, for curation")
    p.add_argument("prompt_id")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--notable", action="store_true", help="only the ones marked notable")
    p.set_defaults(func=cmd_quotes)

    p = sub.add_parser("spotcheck", help="export a random sample for a human to hand-check")
    p.add_argument("prompt_id")
    p.add_argument("--fraction", type=float, default=0.10)
    p.set_defaults(func=cmd_spotcheck)

    p = sub.add_parser("agreement", help="stability re-tag and human spot-check figures")
    p.add_argument("prompt_id")
    p.set_defaults(func=cmd_agreement)

    p = sub.add_parser("export", help="write site/data/qualitative.json")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (codebooks.CodebookError, Locked, ValueError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
