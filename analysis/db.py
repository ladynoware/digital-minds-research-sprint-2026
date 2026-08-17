"""Read the interview data; write the coding results.

Reads follow the rule ``export_site_data.py`` already established: the runner
owns DuckDB's single read-write connection while a fleet is in flight, so a
read falls back to the snapshot copy and a mid-run analysis is safe.

Writes cannot fall back. ``reply_codes`` belongs in the real database, because
the house rule is that every count quoted in prose has to be queryable there —
a table living in a snapshot would be discarded on the next refresh. So a write
refuses, loudly, while the runner holds the file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DB = REPO_ROOT / "data" / "whoami.duckdb"
SNAPSHOT_DB = REPO_ROOT / "data" / "dashboard_snapshot.duckdb"

# `pass_label` and `raw_ref` are additions to the column list in the analysis
# brief, and both are forced by requirements in the same brief:
#
#   * the stability check re-tags a sample a second time with the same model
#     and the same codebook, so two rows for one turn must be distinguishable
#     by something other than their content;
#   * every other row in this project points back at the raw JSONL record of
#     the call that produced it, and a tagging call is no less a measurement
#     than an interview turn.
#
# `cost_usd` follows the same convention as `turns.cost_usd` — read from
# OpenRouter's usage data, never from a hardcoded price table.
SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS reply_code_id_seq START 1;

CREATE TABLE IF NOT EXISTS reply_codes (
    reply_code_id   BIGINT PRIMARY KEY DEFAULT nextval('reply_code_id_seq'),
    turn_id         INTEGER NOT NULL,
    prompt_id       TEXT NOT NULL,
    pass_label      TEXT NOT NULL DEFAULT 'primary',
    codes           TEXT[] NOT NULL,
    flagged_quote   TEXT,
    notable         BOOLEAN,
    tagger_model    TEXT NOT NULL,
    codebook_hash   TEXT NOT NULL,
    raw_ref         TEXT,
    cost_usd        DECIMAL(18, 10),
    created_at      TIMESTAMP NOT NULL,
    UNIQUE (turn_id, pass_label)
);

CREATE INDEX IF NOT EXISTS reply_codes_prompt_idx ON reply_codes (prompt_id);
CREATE INDEX IF NOT EXISTS reply_codes_turn_idx ON reply_codes (turn_id);
"""


class Locked(RuntimeError):
    """The runner holds the database and the operation needs to write."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def connect_read(db: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open for reading, falling back to the runner's snapshot if it is locked."""
    target = db or LIVE_DB
    try:
        return duckdb.connect(str(target), read_only=True)
    except duckdb.IOException:
        if db is not None or not SNAPSHOT_DB.exists():
            raise
        print(f"  live database is locked — reading {SNAPSHOT_DB.name}")
        return duckdb.connect(str(SNAPSHOT_DB), read_only=True)


def connect_write(db: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open for writing. Refuses rather than silently writing to a snapshot."""
    target = db or LIVE_DB
    try:
        con = duckdb.connect(str(target))
    except duckdb.IOException as exc:
        raise Locked(
            f"cannot write to {target}: the runner (or a dashboard) holds it.\n"
            "reply_codes has to land in the real database, not a snapshot — stop\n"
            "the runner and any `whoami dashboard`/`browse` process, then retry."
        ) from exc
    con.execute(SCHEMA)
    return con


def has_reply_codes(con: duckdb.DuckDBPyConnection) -> bool:
    """Has any tagging run yet?

    Reads happen on a read-only connection, which cannot create the table, and
    every read path has to work before the first tagging pass — `status`,
    `export` and `agreement` are all things you run to find out that nothing
    has been coded yet.
    """
    found = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'reply_codes'"
    ).fetchone()
    return found is not None


def rows(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]
