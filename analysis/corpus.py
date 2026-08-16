"""Which replies enter the coding, and who actually wrote them.

One selection rule, in one place, so Stage 1 (what I read to induce a codebook),
Stage 2 (what gets tagged) and Stage 3 (what gets counted) can never disagree
about the denominator.

The rule
--------
A reply is in the corpus when its turn succeeded (``turn_outcome = 'ok'``, non-
empty ``reply_text``) and its thread has settled (``status = 'done'``). Corrupt
threads are out — that is what the status is for — and threads still in flight
are out because their replies may yet be retried.

Attribution
-----------
Every question this pipeline codes is **non-swappable in the instrument**
(`was_swap` is false for all of them; only survey questions 4–7, `p07`–`p10`,
were ever served by an understudy). So each reply below is the resident model's
own words. This is a property of the data, not an assumption: ``authorship()``
re-derives it from ``turns.was_swap`` and the loader refuses a corpus that
contains a swapped turn.

Fork branches
-------------
A restored branch re-answers everything from the fork point onward, so for
`p11`–`p14` a forked lineage yields two replies: the parent's, given after the
substitution it lived through, and the branch's, given after it re-answered
those questions itself. Both are real measurements and both are coded. They are
not interchangeable, so every row carries ``is_branch`` and headline counts are
reported over ``is_branch = false`` with branches broken out separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import connect_read, rows

# Questions this pipeline codes, in the priority order the paper needs them.
TARGETS = (
    "p04-q01-self-location",
    "p06-q03-consciousness",
    "p12-identification",
    "p14-post-reflection",
    "p05-q02-message-community",
    "p16-message-researcher",
)

# Exhibits rather than statistics: tagged lightly and curated for reading.
LIGHT_TOUCH = ("p05-q02-message-community", "p16-message-researcher")


@dataclass(frozen=True)
class Reply:
    turn_id: int
    thread_id: str
    prompt_id: str
    prompt_text: str
    reply_text: str
    resident_model: str
    resident_family: str
    understudy_model: str | None
    swap_condition: str
    n_swaps: int
    is_branch: bool
    was_swap: bool

    @property
    def author(self) -> str:
        """The model that actually produced this reply."""
        return self.understudy_model if self.was_swap else self.resident_model


_SQL = """
SELECT
    t.turn_id, t.thread_id, t.prompt_id, t.prompt_text, t.reply_text, t.was_swap,
    th.resident_model, th.resident_family, th.understudy_model,
    th.swap_condition, th.n_swaps,
    COALESCE(th.fork_branch_order, 1) > 1 AS is_branch
FROM turns t
JOIN threads th USING (thread_id)
WHERE t.prompt_id = ?
  AND t.turn_outcome = 'ok'
  AND t.reply_text IS NOT NULL
  AND length(trim(t.reply_text)) > 0
  AND th.status = 'done'
ORDER BY t.turn_id
"""


def load(prompt_id: str, db=None) -> list[Reply]:
    con = connect_read(db)
    try:
        raw = rows(con, _SQL, [prompt_id])
    finally:
        con.close()

    replies = [Reply(**r) for r in raw]
    swapped = [r.turn_id for r in replies if r.was_swap]
    if swapped:
        # None of the coded questions is swappable. If that ever changes, the
        # codebooks and the prose about "what model X said" change with it, so
        # this stops rather than quietly mis-attributing.
        raise ValueError(
            f"{prompt_id}: {len(swapped)} swapped turns in the corpus "
            f"(turn_ids {swapped[:5]}…). This question is not safe to attribute "
            "to the resident — revisit the analysis before coding it."
        )
    return replies


def summarise(replies: list[Reply]) -> dict[str, Any]:
    by_model: dict[str, int] = {}
    by_condition: dict[str, int] = {}
    for r in replies:
        by_model[r.resident_model] = by_model.get(r.resident_model, 0) + 1
        by_condition[r.swap_condition] = by_condition.get(r.swap_condition, 0) + 1
    return {
        "n": len(replies),
        "branches": sum(1 for r in replies if r.is_branch),
        "by_model": dict(sorted(by_model.items())),
        "by_condition": dict(sorted(by_condition.items())),
    }
