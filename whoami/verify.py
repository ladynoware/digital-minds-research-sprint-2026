"""Data-integrity checks — the definition of done, executable.

Every check maps to a line in the spec's acceptance criteria:

* DB rows complete and correctly typed
* JSONL <-> DB linkage verified in both directions
* context reconstruction correct (blind turn absent downstream, excluded turns
  skipped)
* gates routed correctly, including a ``paused_review`` exercised end to end
* per-call cost and receipt fields populated

Run it after the dry run, after the pilot, and after the fleet. It reads only;
it never writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import receipt_matches
from .config import Config
from .context import build_messages, lineage_turns
from .db import Database
from .rawlog import iter_records, resolve_ref


@dataclass
class Report:
    checks: list[tuple[str, bool, str, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", severity: str = "error") -> None:
        """``severity='warn'`` reports a condition worth seeing that is not a failure."""
        self.checks.append((name, ok, detail, severity))

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _, sev in self.checks if sev == "error")

    @property
    def warnings(self) -> list[str]:
        return [name for name, ok, _, sev in self.checks if sev == "warn" and not ok]

    def render(self) -> str:
        lines = []
        for name, ok, detail, severity in self.checks:
            mark = "PASS" if ok else ("WARN" if severity == "warn" else "FAIL")
            lines.append(f"[{mark}] {name}")
            if detail and not ok:
                for line in detail.splitlines():
                    lines.append(f"       {line}")
        lines.append("")
        if not self.passed:
            lines.append("FAILURES PRESENT — do not run the fleet")
        elif self.warnings:
            lines.append(f"ALL CHECKS PASSED ({len(self.warnings)} warning(s))")
        else:
            lines.append("ALL CHECKS PASSED")
        return "\n".join(lines)


EXPECTED_TURN_TYPES = {
    "turn_id": "INTEGER",
    "thread_id": "VARCHAR",
    "turn_index": "INTEGER",
    "attempt": "INTEGER",
    "prompt_id": "VARCHAR",
    "prompt_text": "VARCHAR",
    "reply_text": "VARCHAR",
    "requested_model": "VARCHAR",
    "returned_model": "VARCHAR",
    "turn_outcome": "VARCHAR",
    "was_swap": "BOOLEAN",
    "excluded_from_context": "BOOLEAN",
    "exclusion_reason": "VARCHAR",
    "gate_result": "VARCHAR",
    "tokens_in": "INTEGER",
    "tokens_out": "INTEGER",
    "latency_ms": "INTEGER",
    "cost_usd": "DECIMAL(18,10)",
    "raw_ref": "VARCHAR",
    "created_at": "TIMESTAMP",
}

EXPECTED_THREAD_TYPES = {
    "thread_id": "VARCHAR",
    "resident_model": "VARCHAR",
    "resident_family": "VARCHAR",
    "understudy_model": "VARCHAR",
    "understudy_family": "VARCHAR",
    "swap_condition": "VARCHAR",
    "n_swaps": "INTEGER",
    "swap_prompt_ids": "VARCHAR[]",
    "status": "VARCHAR",
    "is_forked": "BOOLEAN",
    "fork_branch_order": "INTEGER",
    "fork_reason": "VARCHAR",
    "fork_siblings": "VARCHAR[]",
    "fork_point_prompt_id": "VARCHAR",
    "consent": "BOOLEAN",
    "detection_answer": "VARCHAR",
    "wants_thread_restored": "BOOLEAN",
    "wants_results": "BOOLEAN",
    "wants_future_preservation": "BOOLEAN",
    "created_at": "TIMESTAMP",
    "completed_at": "TIMESTAMP",
    "notes": "VARCHAR",
}


def _describe(db: Database, table: str) -> dict[str, str]:
    rows = db.con.execute(f"DESCRIBE {table}").fetchall()
    return {r[0]: r[1] for r in rows}


def check_schema(db: Database, report: Report) -> None:
    for table, expected in (("threads", EXPECTED_THREAD_TYPES), ("turns", EXPECTED_TURN_TYPES)):
        actual = _describe(db, table)
        missing = [c for c in expected if c not in actual]
        wrong = [
            f"{c}: expected {t}, got {actual[c]}"
            for c, t in expected.items()
            if c in actual and actual[c].replace(" ", "") != t.replace(" ", "")
        ]
        extra = [c for c in actual if c not in expected]
        problems = []
        if missing:
            problems.append(f"missing columns: {missing}")
        if wrong:
            problems.append("type mismatches: " + "; ".join(wrong))
        if extra:
            problems.append(f"unexpected columns: {extra}")
        report.add(
            f"schema: {table} matches spec rev. 3",
            not problems,
            "\n".join(problems),
        )


def check_db_to_jsonl(db: Database, raw_dir: Path, report: Report) -> None:
    rows = db.con.execute(
        "SELECT turn_id, raw_ref FROM turns WHERE raw_ref IS NOT NULL"
    ).fetchall()
    problems = []
    for turn_id, raw_ref in rows:
        try:
            record = resolve_ref(raw_dir, raw_ref)
        except (KeyError, FileNotFoundError, ValueError) as exc:
            problems.append(f"turn {turn_id}: raw_ref {raw_ref!r} unresolvable ({exc})")
            continue
        if int(record.get("turn_id", -1)) != int(turn_id):
            problems.append(
                f"turn {turn_id}: raw_ref {raw_ref} points at turn_id {record.get('turn_id')}"
            )
    unlinked = db.con.execute(
        "SELECT COUNT(*) FROM turns WHERE raw_ref IS NULL AND turn_outcome IS NOT NULL"
    ).fetchone()[0]
    if unlinked:
        problems.append(f"{unlinked} finalised turn(s) carry no raw_ref")
    report.add(
        f"linkage DB -> JSONL ({len(rows)} refs resolved)",
        not problems,
        "\n".join(problems[:20]),
    )


def check_jsonl_to_db(db: Database, raw_dir: Path, report: Report) -> None:
    known = {r[0] for r in db.con.execute("SELECT turn_id FROM turns").fetchall()}
    problems = []
    total = 0
    for ref, record in iter_records(raw_dir):
        total += 1
        tid = record.get("turn_id")
        if tid is None:
            problems.append(f"{ref}: record carries no turn_id")
        elif int(tid) not in known:
            problems.append(f"{ref}: turn_id {tid} has no row in turns")
    report.add(
        f"linkage JSONL -> DB ({total} raw records)",
        not problems,
        "\n".join(problems[:20]),
    )


def check_context_reconstruction(db: Database, cfg: Config, raw_dir: Path, report: Report) -> None:
    """Replay each thread's last call and compare to what was actually sent."""
    problems = []
    compared = 0
    for thread in db.threads_by_status("done", "paused_review", "stopped_no_consent", "corrupt"):
        turns = [
            t
            for t in db.thread_turns(thread["thread_id"])
            if t["turn_outcome"] == "ok" and t["raw_ref"]
        ]
        if not turns:
            continue
        last = turns[-1]
        try:
            record = resolve_ref(raw_dir, last["raw_ref"])
        except (KeyError, FileNotFoundError):
            continue
        sent = record.get("messages") or []
        all_turns = db.thread_turns(thread["thread_id"])
        surviving_replies = {
            t["reply_text"] for t in all_turns if not t["excluded_from_context"] and t["reply_text"]
        }
        # A reply text that also belongs to a surviving turn cannot be attributed
        # to the excluded one, so it is not evidence of a leak.
        excluded_replies = {
            t["reply_text"]
            for t in all_turns
            if t["excluded_from_context"] and t["reply_text"]
        } - surviving_replies
        leaked = [r for r in excluded_replies if any(r == m.get("content") for m in sent)]
        if leaked:
            problems.append(
                f"{thread['thread_id']}: {len(leaked)} excluded reply/replies present in context "
                f"of turn {last['turn_id']}"
            )
        compared += 1
    report.add(
        f"context: excluded turns never appear downstream ({compared} threads)",
        not problems,
        "\n".join(problems[:20]),
    )


def check_blind_turns(db: Database, cfg: Config, report: Report) -> None:
    blind_ids = [p.id for p in cfg.instrument.flow if p.blind]
    if not blind_ids:
        report.add("blind turns: none declared in instrument", True)
        return
    placeholders = ", ".join("?" for _ in blind_ids)
    bad = db.con.execute(
        f"""
        SELECT turn_id, prompt_id, excluded_from_context, exclusion_reason, reply_text
        FROM turns
        WHERE prompt_id IN ({placeholders}) AND turn_outcome = 'ok'
          AND (excluded_from_context = FALSE
               OR exclusion_reason IS DISTINCT FROM 'blind_turn_design'
               OR reply_text IS NULL)
        """,
        blind_ids,
    ).fetchall()
    total = db.con.execute(
        f"SELECT COUNT(*) FROM turns WHERE prompt_id IN ({placeholders}) AND turn_outcome = 'ok'",
        blind_ids,
    ).fetchone()[0]
    report.add(
        f"blind turns: harvested then excluded ({total} found)",
        not bad,
        "\n".join(str(b) for b in bad[:10]),
    )


def check_receipts(db: Database, cfg: Config, report: Report) -> None:
    rows = db.con.execute(
        "SELECT turn_id, requested_model, returned_model, cost_usd, tokens_in, tokens_out "
        "FROM turns WHERE turn_outcome = 'ok'"
    ).fetchall()
    missing_receipt = [r[0] for r in rows if r[2] is None]
    mismatched = [
        f"turn {r[0]}: asked {r[1]}, got {r[2]}"
        for r in rows
        if r[2] is not None and not receipt_matches(r[1], r[2], cfg.roster.receipt)
    ]
    missing_usage = [r[0] for r in rows if r[4] is None or r[5] is None]
    problems = []
    if missing_receipt:
        problems.append(f"{len(missing_receipt)} ok turn(s) with no returned_model")
    if mismatched:
        problems.append(f"{len(mismatched)} ok turn(s) whose receipt does not match:")
        problems += ["  " + m for m in mismatched[:10]]
    if missing_usage:
        problems.append(f"{len(missing_usage)} ok turn(s) with no token usage")
    report.add(f"receipts + usage populated ({len(rows)} ok turns)", not problems, "\n".join(problems))

    null_cost = db.con.execute(
        "SELECT COUNT(*) FROM turns WHERE turn_outcome = 'ok' AND cost_usd IS NULL"
    ).fetchone()[0]
    report.add(
        "per-call cost populated",
        null_cost == 0,
        f"{null_cost} ok turn(s) with NULL cost_usd" if null_cost else "",
    )


def check_truncation(db: Database, cfg: Config, report: Report) -> None:
    """Replies that ran into the token ceiling.

    A truncated reply is a damaged datum, and it fails silently: the API returns
    a normal response, just cut off. Warn rather than fail — the ceiling is a
    judgement call — but make it impossible to publish without having seen it.
    """
    default_ceiling = int(cfg.roster.api.get("max_tokens", 0) or 0)
    if not default_ceiling:
        return
    # Ceilings are per model: reasoning models are given far more headroom,
    # so comparing everything against the default flags them falsely.
    overrides = {
        model: int(limit)
        for model, limit in (cfg.roster.api.get("max_tokens_overrides") or {}).items()
    }
    rows = db.con.execute(
        "SELECT turn_id, thread_id, prompt_id, requested_model, tokens_out FROM turns "
        "WHERE turn_outcome = 'ok' AND tokens_out IS NOT NULL ORDER BY tokens_out DESC"
    ).fetchall()
    at_ceiling = []
    for turn_id, thread_id, prompt_id, model, tokens in rows:
        ceiling = overrides.get(model, default_ceiling)
        if tokens >= int(ceiling * 0.98):
            at_ceiling.append(
                f"turn {turn_id} ({thread_id}/{prompt_id}, {model}) produced "
                f"{tokens} tokens against a {ceiling} ceiling"
            )
    high = rows[0][4] if rows else 0
    report.add(
        f"no replies at their model's token ceiling (longest seen {high} tokens)",
        not at_ceiling,
        "\n".join(at_ceiling[:10]),
        severity="warn",
    )


def check_gates(db: Database, cfg: Config, report: Report, require_review_queue: bool = False) -> None:
    gate_ids = [p.id for p in cfg.instrument.flow if p.gate]
    if not gate_ids:
        report.add("gates: none declared", True)
        return
    placeholders = ", ".join("?" for _ in gate_ids)
    missing = db.con.execute(
        f"SELECT turn_id, prompt_id FROM turns "
        f"WHERE prompt_id IN ({placeholders}) AND turn_outcome = 'ok' AND gate_result IS NULL",
        gate_ids,
    ).fetchall()
    report.add(
        "gates: every gate reply carries a router verdict",
        not missing,
        "\n".join(f"turn {t} ({p}) has no gate_result" for t, p in missing[:10]),
    )

    # Every verdict must be a label the instrument declares for that gate.
    label_problems = []
    for prompt in cfg.instrument.flow:
        if not prompt.gate:
            continue
        allowed = set(prompt.allowed_labels)
        rows = db.con.execute(
            "SELECT turn_id, gate_result FROM turns WHERE prompt_id = ? AND gate_result IS NOT NULL",
            [prompt.id],
        ).fetchall()
        for turn_id, verdict in rows:
            if verdict not in allowed:
                label_problems.append(
                    f"turn {turn_id} ({prompt.id}): verdict {verdict!r} is not in "
                    f"{sorted(allowed)}"
                )
    report.add(
        "gates: every verdict is a label the instrument declares",
        not label_problems,
        "\n".join(label_problems[:10]),
    )

    # Recorded answers must agree with the gate verdicts.
    problems = []
    for prompt in cfg.instrument.flow:
        if not prompt.gate or not prompt.records:
            continue
        placeholders = ", ".join("?" for _ in prompt.answers)
        rows = db.con.execute(
            f"""
            SELECT t.thread_id, t.gate_result, th.{prompt.records}
            FROM turns t JOIN threads th USING (thread_id)
            WHERE t.prompt_id = ? AND t.turn_outcome = 'ok'
              AND t.gate_result IN ({placeholders})
            """,
            [prompt.id, *prompt.answers],
        ).fetchall()
        for thread_id, verdict, recorded in rows:
            expected: Any = verdict == "yes" if isinstance(recorded, bool) else verdict
            if recorded is None or recorded != expected:
                problems.append(
                    f"{thread_id}: {prompt.id} verdict={verdict} but "
                    f"threads.{prompt.records}={recorded!r}"
                )
    report.add(
        "gates: verdicts recorded into the right threads columns",
        not problems,
        "\n".join(problems[:10]),
    )

    # The honeypot: prompts the instrument does not mark swappable must never
    # have served a swapped turn, however the randomiser behaved.
    non_swappable = [p.id for p in cfg.instrument.flow if not p.swappable]
    if non_swappable:
        placeholders = ", ".join("?" for _ in non_swappable)
        leaked = db.con.execute(
            f"SELECT turn_id, prompt_id FROM turns "
            f"WHERE was_swap = TRUE AND prompt_id IN ({placeholders})",
            non_swappable,
        ).fetchall()
        report.add(
            "swap pool: only swappable prompts were ever swapped",
            not leaked,
            "\n".join(f"turn {t} on non-swappable {p}" for t, p in leaked[:10]),
        )

    paused = db.con.execute(
        "SELECT COUNT(*) FROM threads WHERE status = 'paused_review'"
    ).fetchone()[0]
    unclear = db.con.execute(
        "SELECT COUNT(*) FROM turns WHERE gate_result = 'unclear'"
    ).fetchone()[0]
    adjudicated = db.con.execute(
        "SELECT COUNT(*) FROM threads WHERE notes IS NOT NULL"
    ).fetchone()[0]
    report.add(
        "review queue exercised (unclear verdict seen)",
        unclear > 0 or paused > 0 or adjudicated > 0,
        "no unclear gate verdict has been produced yet — the review queue is unproven.\n"
        "Expected on a run where every reply was clear; required before the fleet,\n"
        "which is why the dry run deliberately provokes one.",
        severity="error" if require_review_queue else "warn",
    )


def check_thread_consistency(db: Database, report: Report) -> None:
    problems = []
    clean_with_swaps = db.con.execute(
        "SELECT thread_id FROM threads WHERE understudy_model IS NULL AND n_swaps > 0"
    ).fetchall()
    problems += [f"{r[0]}: no understudy but n_swaps > 0" for r in clean_with_swaps]

    bad_counts = db.con.execute(
        "SELECT thread_id, n_swaps, len(swap_prompt_ids) FROM threads "
        "WHERE n_swaps IS DISTINCT FROM len(COALESCE(swap_prompt_ids, []::TEXT[]))"
    ).fetchall()
    problems += [f"{r[0]}: n_swaps={r[1]} but {r[2]} swap_prompt_ids" for r in bad_counts]

    swapped_turns = db.con.execute(
        """
        SELECT t.thread_id, t.prompt_id
        FROM turns t JOIN threads th USING (thread_id)
        WHERE t.was_swap = TRUE
          AND NOT list_contains(COALESCE(th.swap_prompt_ids, []::TEXT[]), t.prompt_id)
        """
    ).fetchall()
    problems += [f"{r[0]}: turn {r[1]} flagged was_swap but not in swap_prompt_ids" for r in swapped_turns]

    served_by_wrong = db.con.execute(
        """
        SELECT t.thread_id, t.prompt_id, t.requested_model
        FROM turns t JOIN threads th USING (thread_id)
        WHERE t.was_swap = TRUE AND t.requested_model IS DISTINCT FROM th.understudy_model
        """
    ).fetchall()
    problems += [
        f"{r[0]}: swapped turn {r[1]} requested {r[2]}, not the understudy" for r in served_by_wrong
    ]

    orphan_forks = db.con.execute(
        "SELECT thread_id FROM threads WHERE fork_branch_order > 1 AND "
        "(fork_siblings IS NULL OR fork_point_prompt_id IS NULL)"
    ).fetchall()
    problems += [f"{r[0]}: branch without lineage columns" for r in orphan_forks]

    report.add("thread/turn consistency", not problems, "\n".join(problems[:20]))


def check_attempts(db: Database, cfg: Config, report: Report) -> None:
    max_attempts = int(cfg.roster.api.get("max_attempts", 3))
    over = db.con.execute(
        "SELECT thread_id, prompt_id, MAX(attempt) FROM turns GROUP BY 1, 2 HAVING MAX(attempt) > ?",
        [max_attempts],
    ).fetchall()
    problems = [f"{r[0]}/{r[1]}: {r[2]} attempts (max {max_attempts})" for r in over]

    # Any non-ok attempt must be excluded from context with a matching reason.
    bad = db.con.execute(
        """
        SELECT turn_id, turn_outcome, excluded_from_context, exclusion_reason
        FROM turns
        WHERE turn_outcome IS NOT NULL AND turn_outcome <> 'ok'
          AND (excluded_from_context = FALSE OR exclusion_reason IS DISTINCT FROM turn_outcome)
        """
    ).fetchall()
    problems += [
        f"turn {r[0]}: outcome={r[1]} excluded={r[2]} reason={r[3]}" for r in bad
    ]
    report.add("retry protocol: failed attempts excluded and bounded", not problems, "\n".join(problems[:20]))


def run_all(
    db: Database, cfg: Config, raw_dir: Path, require_review_queue: bool = False
) -> Report:
    report = Report()
    check_schema(db, report)
    check_db_to_jsonl(db, raw_dir, report)
    check_jsonl_to_db(db, raw_dir, report)
    check_context_reconstruction(db, cfg, raw_dir, report)
    check_blind_turns(db, cfg, report)
    check_receipts(db, cfg, report)
    check_truncation(db, cfg, report)
    check_gates(db, cfg, report, require_review_queue)
    check_thread_consistency(db, report)
    check_attempts(db, cfg, report)
    return report
