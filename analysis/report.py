"""Counts and validation — the numbers Stage 3 writes prose against.

House rule: every count quoted in prose has to be queryable in the database.
So the prose is written from what this module returns, and this module is
nothing but SQL over ``reply_codes`` joined back to ``threads``. Nothing is
recomputed in Python that could have been counted in DuckDB.

Two denominators, kept apart on purpose. Headline rates are over the primary
stratum — one reply per thread lineage, excluding restored branches — because a
forked lineage answers `p11`–`p14` twice and counting both would weight those
lineages double. Branch replies are reported separately, where the comparison
between a parent's answer and its branch's answer is the interesting thing
rather than a nuisance.

**The condition trap.** A fork branch inherits its parent's ``swap_condition``
but has ``n_swaps = 0`` — it re-answered those questions from its own weights,
which is the whole point of the restoration. So ``swap_condition`` alone does
not mean "this thread was swapped", and grouping on it without excluding
branches counts 23 unswapped threads as swapped. Flagged by the runner, and
recorded in the README's analysis caveat. Two defences here: the condition
breakdown runs over the primary stratum only, and ``was_swapped`` is carried
alongside as the honest predicate (``n_swaps > 0``) for anything that needs it.
``counts`` also audits the stratum and reports any row where the two disagree.
"""

from __future__ import annotations

import random
from typing import Any

from .db import connect_read, has_reply_codes, rows

# One row per reply, with the thread facts needed to break the counts down.
_BASE = """
SELECT
    rc.turn_id, rc.prompt_id, rc.codes, rc.flagged_quote, rc.notable,
    t.reply_text, t.thread_id,
    th.resident_model, th.resident_family, th.swap_condition, th.n_swaps,
    -- `swap_condition` is inherited by branches; `n_swaps` is not. This is the
    -- predicate that actually means "a foreign turn happened in this thread".
    th.n_swaps > 0 AS was_swapped,
    COALESCE(th.fork_branch_order, 1) > 1 AS is_branch
FROM reply_codes rc
JOIN turns t USING (turn_id)
JOIN threads th ON th.thread_id = t.thread_id
WHERE rc.prompt_id = ? AND rc.pass_label = 'primary'
"""


def _tally(records: list[dict], key: str | None = None) -> dict[str, Any]:
    """Code counts over a set of replies, with the denominator that produced them."""
    n = len(records)
    counts: dict[str, int] = {}
    for r in records:
        for code in r["codes"]:
            counts[code] = counts.get(code, 0) + 1
    ordered = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    out: dict[str, Any] = {
        "n": n,
        "counts": ordered,
        "pct": {c: round(100 * v / n, 1) for c, v in ordered.items()} if n else {},
    }
    if key:
        out["key"] = key
    return out


def counts(prompt_id: str, db=None) -> dict[str, Any]:
    con = connect_read(db)
    try:
        records = rows(con, _BASE, [prompt_id]) if has_reply_codes(con) else []
    finally:
        con.close()

    primary = [r for r in records if not r["is_branch"]]
    branches = [r for r in records if r["is_branch"]]

    def grouped(field: str) -> dict[str, Any]:
        buckets: dict[str, list[dict]] = {}
        for r in primary:
            buckets.setdefault(r[field], []).append(r)
        return {k: _tally(v, k) for k, v in sorted(buckets.items())}

    overall = _tally(primary)
    other_pct = overall["pct"].get("other", 0.0)

    # The audit for the condition trap: in the primary stratum a non-clean
    # thread should always carry at least one swap. A row where it does not
    # means either a branch slipped into the stratum or the design moved.
    mislabelled = [
        r["thread_id"]
        for r in primary
        if r["swap_condition"] != "clean" and not r["was_swapped"]
    ]

    return {
        "prompt_id": prompt_id,
        "overall": overall,
        "by_model": grouped("resident_model"),
        "by_family": grouped("resident_family"),
        "by_condition": grouped("swap_condition"),
        # Kept alongside the condition breakdown rather than derived from it,
        # so a swapped/unswapped comparison never has to trust the label.
        "by_swapped": {
            ("swapped" if k else "not_swapped"): v
            for k, v in {
                key: _tally([r for r in primary if r["was_swapped"] == key])
                for key in (True, False)
            }.items()
        },
        "branches": _tally(branches) if branches else None,
        "condition_label_conflicts": mislabelled,
        # The brief's own quality gate on the codebook, computed rather than asserted.
        "other_share_pct": other_pct,
        "other_within_ceiling": other_pct <= 10.0,
        "multi_label_mean": (
            round(sum(len(r["codes"]) for r in primary) / len(primary), 2) if primary else 0.0
        ),
    }


def quotes(prompt_id: str, limit: int = 40, notable_only: bool = False, db=None) -> list[dict]:
    """Flagged quotes, newest-model-agnostic, for curation and for the summaries."""
    con = connect_read(db)
    try:
        records = rows(con, _BASE, [prompt_id]) if has_reply_codes(con) else []
    finally:
        con.close()
    picked = [
        r
        for r in records
        if r["flagged_quote"] and (r["notable"] or not notable_only)
    ]
    picked.sort(key=lambda r: (not r["notable"], r["thread_id"]))
    return [
        {
            "thread_id": r["thread_id"],
            "turn_id": r["turn_id"],
            "model": r["resident_model"],
            "condition": r["swap_condition"],
            "codes": list(r["codes"]),
            "quote": r["flagged_quote"],
            "notable": bool(r["notable"]),
        }
        for r in picked[:limit]
    ]


def spotcheck_sample(prompt_id: str, fraction: float = 0.10, seed: int = 20260816, db=None) -> list[dict]:
    """A random sample of (reply, assigned codes) for a human to check by hand.

    Random rather than curated, and seeded rather than ad hoc, so the agreement
    figure the paper reports is over a sample nobody chose.
    """
    con = connect_read(db)
    try:
        records = rows(con, _BASE, [prompt_id]) if has_reply_codes(con) else []
    finally:
        con.close()
    if not records:
        return []
    k = max(1, round(len(records) * fraction))
    sample = random.Random(seed).sample(records, min(k, len(records)))
    sample.sort(key=lambda r: r["turn_id"])
    return sample


def agreement(prompt_id: str, db=None) -> dict[str, Any]:
    """Code-level agreement between the primary pass and the stability re-tag.

    Self-consistency, reported as the poor-man's inter-rater reliability it is.
    Per reply: Jaccard over the two code sets, plus exact-set match. Reported as
    a mean over replies rather than pooled over codes, so one long code list
    cannot dominate.
    """
    con = connect_read(db)
    try:
        if not has_reply_codes(con):
            return {"prompt_id": prompt_id, "n": 0}
        pairs = rows(
            con,
            """
            SELECT a.turn_id, a.codes AS primary_codes, b.codes AS stability_codes
            FROM reply_codes a
            JOIN reply_codes b
              ON a.turn_id = b.turn_id AND a.prompt_id = b.prompt_id
            WHERE a.prompt_id = ? AND a.pass_label = 'primary' AND b.pass_label = 'stability'
            ORDER BY a.turn_id
            """,
            [prompt_id],
        )
    finally:
        con.close()

    if not pairs:
        return {"prompt_id": prompt_id, "n": 0}

    jaccards: list[float] = []
    exact = 0
    per_code: dict[str, list[int]] = {}
    for p in pairs:
        a, b = set(p["primary_codes"]), set(p["stability_codes"])
        union = a | b
        jaccards.append(len(a & b) / len(union) if union else 1.0)
        exact += a == b
        for code in union:
            hit = per_code.setdefault(code, [0, 0])
            hit[1] += 1
            hit[0] += code in a and code in b

    return {
        "prompt_id": prompt_id,
        "n": len(pairs),
        "mean_jaccard": round(sum(jaccards) / len(jaccards), 3),
        "exact_set_match_pct": round(100 * exact / len(pairs), 1),
        "per_code_agreement": {
            c: round(100 * hit / total, 1) for c, (hit, total) in sorted(per_code.items())
        },
    }
