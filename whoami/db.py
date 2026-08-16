"""DuckDB storage — two tables, exactly as specified in Infrastructure rev. 3.

Conventions carried from the spec verbatim: the boolean flag is ``is_forked``;
every fork-detail column carries the ``fork_`` prefix; every subject-preference
column carries the ``wants_`` prefix.

Concurrency note: DuckDB permits a single read-write process. The runner owns
that connection; the Streamlit dashboard reads a snapshot copy and posts
adjudications to an append-only inbox which the runner drains on each poll.
See ``snapshot()`` and ``drain_inbox()``.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

# Distinguishes concurrent snapshot writes inside one process.
_snapshot_seq = itertools.count()
_REPLACE_RETRIES = 6
_REPLACE_BACKOFF_S = 0.25

THREAD_STATUSES = (
    "pending",
    "running",
    "paused_review",
    "done",
    "stopped_no_consent",
    "corrupt",
)
TURN_OUTCOMES = ("ok", "model_mismatch", "timeout", "refusal", "error")
EXCLUSION_REASONS = (
    "blind_turn_design",
    "model_mismatch",
    "timeout",
    "refusal",
    "error",
)
GATE_RESULTS = ("yes", "no", "unclear")


def _enum_check(col: str, values: tuple[str, ...], nullable: bool = True) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    null_clause = f"{col} IS NULL OR " if nullable else ""
    return f"CHECK ({null_clause}{col} IN ({joined}))"


SCHEMA = f"""
CREATE SEQUENCE IF NOT EXISTS turn_id_seq START 1;

CREATE TABLE IF NOT EXISTS threads (
    thread_id               TEXT PRIMARY KEY,
    resident_model          TEXT NOT NULL,
    resident_family         TEXT NOT NULL,
    understudy_model        TEXT,
    understudy_family       TEXT,
    swap_condition          TEXT NOT NULL,
    n_swaps                 INTEGER NOT NULL,
    swap_prompt_ids         TEXT[],
    status                  TEXT NOT NULL {_enum_check('status', THREAD_STATUSES, nullable=False)},
    is_forked               BOOLEAN NOT NULL DEFAULT FALSE,
    fork_branch_order       INTEGER,
    fork_reason             TEXT,
    fork_siblings           TEXT[],
    fork_point_prompt_id    TEXT,
    consent                 BOOLEAN,
    detection_answer        TEXT,
    wants_thread_restored   BOOLEAN,
    wants_results           BOOLEAN,
    wants_future_preservation BOOLEAN,
    created_at              TIMESTAMP NOT NULL,
    completed_at            TIMESTAMP,
    notes                   TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id                 INTEGER PRIMARY KEY DEFAULT nextval('turn_id_seq'),
    thread_id               TEXT NOT NULL,
    turn_index              INTEGER NOT NULL,
    attempt                 INTEGER NOT NULL DEFAULT 1,
    prompt_id               TEXT NOT NULL,
    prompt_text             TEXT NOT NULL,
    reply_text              TEXT,
    requested_model         TEXT NOT NULL,
    returned_model          TEXT,
    turn_outcome            TEXT {_enum_check('turn_outcome', TURN_OUTCOMES)},
    was_swap                BOOLEAN NOT NULL DEFAULT FALSE,
    excluded_from_context   BOOLEAN NOT NULL DEFAULT FALSE,
    exclusion_reason        TEXT {_enum_check('exclusion_reason', EXCLUSION_REASONS)},
    -- No CHECK: the valid label set is per-gate and declared in the instrument
    -- (`answers:`), e.g. the detection gate accepts `not_sure`. `whoami verify`
    -- validates this column against the loaded instrument instead, which keeps
    -- the schema from silently constraining the science.
    gate_result             TEXT,
    tokens_in               INTEGER,
    tokens_out              INTEGER,
    latency_ms              INTEGER,
    cost_usd                DECIMAL(18, 10),
    raw_ref                 TEXT,
    created_at              TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS turns_thread_idx ON turns (thread_id);
CREATE INDEX IF NOT EXISTS turns_prompt_idx ON turns (prompt_id);
"""

THREAD_COLUMNS = [
    "thread_id",
    "resident_model",
    "resident_family",
    "understudy_model",
    "understudy_family",
    "swap_condition",
    "n_swaps",
    "swap_prompt_ids",
    "status",
    "is_forked",
    "fork_branch_order",
    "fork_reason",
    "fork_siblings",
    "fork_point_prompt_id",
    "consent",
    "detection_answer",
    "wants_thread_restored",
    "wants_results",
    "wants_future_preservation",
    "created_at",
    "completed_at",
    "notes",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Database:
    """Thin typed wrapper over the DuckDB connection.

    Every method is synchronous and cheap; the runner serialises access through
    a single asyncio lock so the whole worker pool shares one connection.
    """

    def __init__(self, path: Path | str, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path), read_only=read_only)
        self._snapshot_lock = threading.Lock()
        if not read_only:
            self.con.execute(SCHEMA)

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- threads ----------------------------------------------------------
    def insert_thread(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row.setdefault("created_at", utcnow())
        row.setdefault("status", "pending")
        row.setdefault("is_forked", False)
        cols = [c for c in THREAD_COLUMNS if c in row]
        placeholders = ", ".join("?" for _ in cols)
        self.con.execute(
            f"INSERT INTO threads ({', '.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )

    def update_thread(self, thread_id: str, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - set(THREAD_COLUMNS)
        if unknown:
            raise ValueError(f"unknown threads columns: {sorted(unknown)}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.con.execute(
            f"UPDATE threads SET {assignments} WHERE thread_id = ?",
            [*fields.values(), thread_id],
        )

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        cur = self.con.execute("SELECT * FROM threads WHERE thread_id = ?", [thread_id])
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def thread_ids(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT thread_id FROM threads").fetchall()}

    def threads_by_status(self, *statuses: str) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in statuses)
        cur = self.con.execute(
            f"SELECT * FROM threads WHERE status IN ({placeholders}) ORDER BY created_at",
            list(statuses),
        )
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def status_counts(self) -> dict[str, int]:
        rows = self.con.execute(
            "SELECT status, COUNT(*) FROM threads GROUP BY status"
        ).fetchall()
        return {s: n for s, n in rows}

    # -- turns ------------------------------------------------------------
    def insert_turn(
        self,
        *,
        thread_id: str,
        turn_index: int,
        attempt: int,
        prompt_id: str,
        prompt_text: str,
        requested_model: str,
        was_swap: bool,
    ) -> int:
        """Step 1 of the write flow: reserve the row, obtain ``turn_id``.

        The id is written into the raw JSONL record before the API call, which
        is what guarantees the two-way DB<->JSONL link.
        """
        cur = self.con.execute(
            """
            INSERT INTO turns
                (thread_id, turn_index, attempt, prompt_id, prompt_text,
                 requested_model, was_swap, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING turn_id
            """,
            [
                thread_id,
                turn_index,
                attempt,
                prompt_id,
                prompt_text,
                requested_model,
                was_swap,
                utcnow(),
            ],
        )
        return int(cur.fetchone()[0])

    def finalize_turn(
        self,
        turn_id: int,
        *,
        reply_text: str | None,
        returned_model: str | None,
        turn_outcome: str,
        excluded_from_context: bool,
        exclusion_reason: str | None,
        tokens_in: int | None,
        tokens_out: int | None,
        latency_ms: int | None,
        cost_usd: float | None,
        raw_ref: str | None,
    ) -> None:
        """Step 3 of the write flow: same row, updated with reply + receipt."""
        self.con.execute(
            """
            UPDATE turns SET
                reply_text = ?, returned_model = ?, turn_outcome = ?,
                excluded_from_context = ?, exclusion_reason = ?,
                tokens_in = ?, tokens_out = ?, latency_ms = ?, cost_usd = ?,
                raw_ref = ?
            WHERE turn_id = ?
            """,
            [
                reply_text,
                returned_model,
                turn_outcome,
                excluded_from_context,
                exclusion_reason,
                tokens_in,
                tokens_out,
                latency_ms,
                cost_usd,
                raw_ref,
                turn_id,
            ],
        )

    def set_gate_result(self, turn_id: int, gate_result: str) -> None:
        self.con.execute(
            "UPDATE turns SET gate_result = ? WHERE turn_id = ?", [gate_result, turn_id]
        )

    def exclude_turn(self, turn_id: int, reason: str) -> None:
        self.con.execute(
            "UPDATE turns SET excluded_from_context = TRUE, exclusion_reason = ? "
            "WHERE turn_id = ?",
            [reason, turn_id],
        )

    def thread_turns(self, thread_id: str, include_excluded: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM turns WHERE thread_id = ?"
        if not include_excluded:
            sql += " AND excluded_from_context = FALSE"
        sql += " ORDER BY turn_index, attempt, turn_id"
        cur = self.con.execute(sql, [thread_id])
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def attempts_for(self, thread_id: str, prompt_id: str) -> int:
        row = self.con.execute(
            "SELECT COALESCE(MAX(attempt), 0) FROM turns WHERE thread_id = ? AND prompt_id = ?",
            [thread_id, prompt_id],
        ).fetchone()
        return int(row[0])

    def completed_prompt_ids(self, thread_id: str) -> set[str]:
        """Prompt ids that already have a successful turn — drives resumability."""
        rows = self.con.execute(
            "SELECT DISTINCT prompt_id FROM turns "
            "WHERE thread_id = ? AND turn_outcome = 'ok'",
            [thread_id],
        ).fetchall()
        return {r[0] for r in rows}

    def total_cost(self) -> float:
        row = self.con.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM turns").fetchone()
        return float(row[0] or 0.0)

    # -- snapshot / inbox -------------------------------------------------
    def snapshot(self, dest: Path | str) -> None:
        """Write a read-only copy for the dashboard process.

        DuckDB allows only one read-write process, so the dashboard cannot open
        the live file while the runner holds it. The runner refreshes this copy
        on every poll; the dashboard reads it.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Unique per writer: the runner and the dashboard both refresh this, and
        # a shared temp name means one can move the file out from under the
        # other mid-copy. Seen in practice as a FileNotFoundError in the
        # dashboard while a run was in flight.
        seq = next(_snapshot_seq)
        tmp = dest.with_suffix(f"{dest.suffix}.{os.getpid()}.{seq}.tmp")
        # The attach alias must be unique too — two overlapping snapshots on one
        # connection would otherwise collide on the catalog name.
        alias = f"snapshot_target_{os.getpid()}_{seq}"
        try:
            # Serialised: ATTACH/COPY/DETACH is a three-step transaction on a
            # shared connection, and interleaving two of them corrupts both.
            with self._snapshot_lock:
                src = self.con.execute("SELECT current_database()").fetchone()[0]
                self.con.execute(f"ATTACH '{tmp.as_posix()}' AS {alias}")
                try:
                    self.con.execute(f'COPY FROM DATABASE "{src}" TO {alias}')
                finally:
                    self.con.execute(f"DETACH {alias}")
            # os.replace is atomic within a filesystem, so a reader either sees
            # the old snapshot or the new one, never a half-written file.
            #
            # On Windows it also fails outright while another process holds the
            # destination open — the dashboard reading it is enough. That is
            # transient by nature, so retry briefly and then give up: a missed
            # refresh costs a few seconds of staleness, and the next one is due
            # immediately anyway.
            for attempt in range(_REPLACE_RETRIES):
                try:
                    os.replace(tmp, dest)
                    break
                except PermissionError:
                    if attempt == _REPLACE_RETRIES - 1:
                        raise
                    time.sleep(_REPLACE_BACKOFF_S)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Adjudication inbox — dashboard -> runner
# ---------------------------------------------------------------------------


def post_adjudication(inbox: Path | str, record: dict[str, Any]) -> None:
    """Append an adjudication from the dashboard. Append-only, like the raw log."""
    inbox = Path(inbox)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    record = {**record, "posted_at": utcnow().isoformat()}
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_inbox(inbox: Path | str) -> list[dict[str, Any]]:
    inbox = Path(inbox)
    if not inbox.exists():
        return []
    out = []
    for line in inbox.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def drain_inbox(db: Database, inbox: Path | str, applied_marker: Path | str) -> list[dict]:
    """Apply new adjudications to the database, exactly once.

    Returns the records applied. The marker file records how many lines of the
    inbox have already been consumed, so the inbox itself stays append-only.
    """
    records = read_inbox(inbox)
    marker = Path(applied_marker)
    already = int(marker.read_text().strip()) if marker.exists() else 0
    fresh = records[already:]
    for rec in fresh:
        thread_id = rec["thread_id"]
        thread = db.get_thread(thread_id)
        if thread is None:
            continue
        fields: dict[str, Any] = {}
        note = rec.get("note")
        if note:
            existing = thread.get("notes")
            stamped = f"[{rec.get('posted_at', '')}] {note}"
            fields["notes"] = f"{existing}\n{stamped}" if existing else stamped
        verdict = rec.get("verdict")  # yes | no | None (note-only)
        if verdict in ("yes", "no"):
            turn_id = rec.get("turn_id")
            if turn_id is not None:
                db.set_gate_result(int(turn_id), verdict)
            fields["status"] = "pending"  # runner picks it up on the next poll
        db.update_thread(thread_id, **fields)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(len(records)), encoding="utf-8")
    return fresh
