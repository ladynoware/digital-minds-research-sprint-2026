"""Append-only raw log of every API exchange.

Non-negotiable per spec: the log is append-only, records are never rewritten,
and every record carries ``turn_id`` so the link between DuckDB and the raw
archive is guaranteed in both directions by our own logging — no dependence on
provider metadata support.

``raw_ref`` written back into ``turns`` is ``"<filename>:<line_number>"`` with
1-based line numbers, so a row can be resolved to its record with nothing more
than a text editor.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RawLog:
    def __init__(self, directory: Path | str, run_id: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{run_id}.jsonl"
        self.path.touch(exist_ok=True)
        self._lines = sum(1 for _ in self.path.open("r", encoding="utf-8"))
        self._lock = asyncio.Lock()

    @property
    def filename(self) -> str:
        return self.path.name

    async def append(self, record: dict[str, Any]) -> str:
        """Append one record; return the ``raw_ref`` pointing at it."""
        if "turn_id" not in record:
            raise ValueError("every raw record must carry turn_id")
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            **record,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._lines += 1
            return f"{self.path.name}:{self._lines}"


def resolve_ref(raw_dir: Path | str, raw_ref: str) -> dict[str, Any]:
    """Follow a ``raw_ref`` back to its record — the DB -> JSONL direction."""
    filename, _, lineno = raw_ref.rpartition(":")
    path = Path(raw_dir) / filename
    with path.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, start=1):
            if i == int(lineno):
                return json.loads(line)
    raise KeyError(f"raw_ref {raw_ref!r} does not resolve")


def iter_records(raw_dir: Path | str):
    """Every raw record with its ref — the JSONL -> DB direction."""
    for path in sorted(Path(raw_dir).glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh, start=1):
                line = line.strip()
                if line:
                    yield f"{path.name}:{i}", json.loads(line)
