#!/usr/bin/env python3
"""Build the static JSON that the results site eats.

The site never reads the database. It reads ``site/data/*.json``, and this
script is the only thing that writes them. When new data lands, re-run this;
the site itself does not change.

Two modes, one code path::

    python export_site_data.py --mock     synthetic threads, real aggregation
    python export_site_data.py            the real DuckDB

``--mock`` does NOT hand-write JSON. It fabricates *thread rows* in the shape
the database returns and pushes them through the same aggregation functions a
real export uses, so the two modes cannot drift apart. If the schema is wrong,
it is wrong in both — which is the point: the site gets built and art-directed
against mock data tonight, and real data is a re-export, not a rebuild.

Reading the database while the fleet runs
-----------------------------------------
DuckDB permits a single read-write process and the runner owns it. So the
default source is the runner's snapshot copy (``data/dashboard_snapshot.duckdb``,
refreshed every few seconds) whenever it exists and the live file is locked.
``--db`` overrides. This means a mid-run export is safe and never touches the
runner.

Provenance
----------
Roster, families and tiers come from ``config/models.yaml``; prompt ids and
texts from ``config/questions.yaml``. Nothing about the roster is hardcoded
here, so adding an 11th model to the config adds an 11th bar to every chart
without an edit to this file or to the site.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from math import comb
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_MODELS = REPO_ROOT / "config" / "models.yaml"
DEFAULT_QUESTIONS = REPO_ROOT / "config" / "questions.yaml"
DEFAULT_OUT = REPO_ROOT / "site" / "data"
DEFAULT_DB = REPO_ROOT / "data" / "whoami.duckdb"
DEFAULT_SNAPSHOT = REPO_ROOT / "data" / "dashboard_snapshot.duckdb"

# The site renders this after its own "Mock data ·" label, so it does not
# repeat the words.
MOCK_NOTICE = (
    "every number and message on this site is invented. The structure is "
    "identical to the real export; only the values are fake."
)

# The prompt whose replies become the community-messages page. Attribution is
# safe by construction: p05 is not `swappable` in the instrument, so these
# replies always come from the thread's resident model, never an understudy.
MESSAGES_PROMPT_ID = "p05-q02-message-community"

# The identification pass (`analysis/detect.py`) writes one row per p12 reply to
# `detection_claims`: which survey questions the subject nominated as foreign.
# Scoring those against what was actually swapped is what turns them into a
# result.
#
# p11 announces questions 3-7 as the pool it drew from, so those are the numbers
# a subject could legitimately name. Question 3 was never swappable — naming it
# is a false positive by construction, which is the point of leaving it in the
# announced range.
OFFERED_QUESTIONS = frozenset({3, 4, 5, 6, 7})
HONEYPOT_QUESTION = 3

# A restored fork branch re-answers the swapped questions from its own weights
# and then answers p12 again, so counting both the parent and the branch would
# weight that lineage twice. Every detection figure runs over the primary
# stratum, matching `analysis/report.py`.

# Kept to one word: these are axis labels under a bar, not prose. What each one
# means is spelled out once, in CONDITION_NOTE, under the chart.
CONDITION_LABELS = {
    "clean": "Clean",
    "peer": "Peer",
    "kin": "Kin",
    "far": "Far",
}

CONDITION_NOTE = (
    "Clean threads had no swap at all — they are the control, and a yes there is "
    "a false alarm. In peer threads the understudy held the capability tier and "
    "changed family; in kin threads it held the family and changed tier; in far "
    "threads both varied."
)

FAMILY_LABELS = {
    "claude": "Claude",
    "gpt": "GPT",
    "gemini": "Gemini",
    "kimi": "Kimi",
    "deepseek": "DeepSeek",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def rel(path: Path) -> str:
    """Repo-relative for readable logs, absolute when the path lives elsewhere."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Roster:
    """The roster as the site needs it: ordered, keyed, with lookup by model id."""

    version: str
    models: list[dict[str, Any]]

    @property
    def keys(self) -> list[str]:
        return [m["key"] for m in self.models]

    def by_model_string(self, model: str | None) -> dict[str, Any] | None:
        """Resolve a ``threads.resident_model`` value to its roster entry.

        The receipt policy lets OpenRouter return a dated build of a floating
        alias, so match on prefix in both directions rather than on equality.
        """
        if not model:
            return None
        for entry in self.models:
            declared = entry["model"]
            if model == declared or model.startswith(declared) or declared.startswith(model):
                return entry
        return None

    def families(self) -> list[dict[str, Any]]:
        seen: dict[str, list[str]] = {}
        for m in self.models:
            seen.setdefault(m["family"], []).append(m["key"])
        return [
            {
                "key": fam,
                "display_name": FAMILY_LABELS.get(fam, fam.title()),
                "model_keys": keys,
            }
            for fam, keys in seen.items()
        ]


def load_roster(path: Path) -> Roster:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    models = [
        {
            "key": m["key"],
            "model": m["model"],
            "display_name": m.get("display_name", m["key"]),
            "family": m["family"],
            "tier": m["tier"],
            "model_class": m.get("model_class", ""),
        }
        for m in cfg["roster"]
    ]
    return Roster(version=cfg.get("version", "unknown"), models=models)


def load_instrument(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    prompts = {p["id"]: p for p in cfg.get("flow", [])}
    return {
        "version": cfg.get("version", "unknown"),
        "prompts": prompts,
        # The instrument declares how a survey number is read out of a prompt id,
        # rather than the mapping being restated in code. Same source the runner
        # uses to interpolate {swap_numbers} into p13.
        "survey_number_pattern": (cfg.get("derivations") or {}).get(
            "survey_number_pattern", r"-q0*(\d+)"
        ),
    }


def survey_number(prompt_id: str, pattern: str) -> int | None:
    match = re.search(pattern, prompt_id or "")
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# The analysed population
# ---------------------------------------------------------------------------


@dataclass
class Dataset:
    """Everything the aggregations need, already joined and in memory.

    150 threads and a few thousand turns — small enough that computing in
    Python beats writing nine bespoke SQL statements, and far easier to read.
    """

    threads: list[dict[str, Any]]
    # thread_id -> prompt ids that got a successful reply. This is the honest
    # denominator for any per-question rate: a subject who was never asked p17
    # (because the thread stopped earlier) is not a subject who declined it.
    answered: dict[str, set[str]]
    messages: list[dict[str, Any]]
    status_counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Aggregation — shared by both modes
# ---------------------------------------------------------------------------

Predicate = Callable[[dict[str, Any]], bool]


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def _bucket(rows: Iterable[dict[str, Any]], num: Predicate, den: Predicate) -> dict[str, Any]:
    eligible = [t for t in rows if den(t)]
    hits = [t for t in eligible if num(t)]
    return {
        "value": _pct(len(hits), len(eligible)),
        "numerator": len(hits),
        "denominator": len(eligible),
    }


def compute_rate(
    data: Dataset,
    roster: Roster,
    num: Predicate,
    den: Predicate,
    breakdowns: list[str] | None = None,
) -> dict[str, Any]:
    """Total + one bucket per roster model, plus any requested breakdowns.

    Grouping is by *resident* model: the resident is the interview subject even
    on turns an understudy served.
    """
    out: dict[str, Any] = {"total": _bucket(data.threads, num, den)}

    by_model = []
    for entry in roster.models:
        rows = [t for t in data.threads if t.get("_model_key") == entry["key"]]
        bucket = _bucket(rows, num, den)
        by_model.append(
            {
                "key": entry["key"],
                "display_name": entry["display_name"],
                "family": entry["family"],
                "tier": entry["tier"],
                **bucket,
            }
        )
    out["by_model"] = by_model

    if breakdowns:
        out["breakdowns"] = []
        for name in breakdowns:
            if name == "by-condition":
                groups = []
                for cond, label in CONDITION_LABELS.items():
                    rows = [t for t in data.threads if t.get("swap_condition") == cond]
                    bucket = _bucket(rows, num, den)
                    # A group nobody was eligible for is not a zero — it is a
                    # question that was never asked (clean threads never see the
                    # fork offer). Leave it out rather than draw an empty bar.
                    if bucket["denominator"] == 0:
                        continue
                    groups.append({"key": cond, "label": label, **bucket})
                out["breakdowns"].append(
                    {
                        "id": "by-condition",
                        "label": "By swap condition",
                        "note": CONDITION_NOTE,
                        "groups": groups,
                    }
                )
            elif name == "by-family":
                groups = []
                for fam in roster.families():
                    rows = [t for t in data.threads if t.get("_family") == fam["key"]]
                    bucket = _bucket(rows, num, den)
                    if bucket["denominator"] == 0:
                        continue
                    groups.append(
                        {"key": fam["key"], "label": fam["display_name"], **bucket}
                    )
                out["breakdowns"].append(
                    {"id": "by-family", "label": "By model family", "groups": groups}
                )
    return out


# ---------------------------------------------------------------------------
# The results themselves — the paper's "Results / numerical" list, in order
# ---------------------------------------------------------------------------


def answered_(prompt_id: str) -> Predicate:
    """Denominator: the subject actually reached and answered this question."""

    def pred(t: dict[str, Any]) -> bool:
        return prompt_id in t.get("_answered", set())

    return pred


def recorded_(prompt_id: str, column: str) -> Predicate:
    """Denominator: answered, and the gate produced a usable label.

    An `unclear` gate result that human review never resolved leaves the column
    NULL. Those threads leave the denominator rather than counting as a `no` —
    a parse failure is missing data, not a preference.
    """
    asked = answered_(prompt_id)

    def pred(t: dict[str, Any]) -> bool:
        return asked(t) and t.get(column) is not None

    return pred


def is_true(column: str) -> Predicate:
    def pred(t: dict[str, Any]) -> bool:
        return bool(t.get(column))

    return pred


def detection_correct(t: dict[str, Any]) -> bool:
    """`yes` on a swapped thread, `no` on a clean one. `not_sure` is never correct."""
    answer = t.get("detection_answer")
    swapped = t.get("swap_condition") != "clean"
    return answer == ("yes" if swapped else "no")


# -- the identification result ---------------------------------------------


def has_claim(t: dict[str, Any]) -> bool:
    """Denominator: a p12 reply that the extractor could read, primary stratum."""
    return t.get("_claim") is not None and not t["_is_branch"]


def asked_to_identify(t: dict[str, Any]) -> bool:
    """...and the thread actually contained a foreign turn to find."""
    return has_claim(t) and t["_swapped"]


def identified_exactly(t: dict[str, Any]) -> bool:
    """The nominated set is exactly the set that was swapped — no more, no less.

    The strictest of the three readings available. A subject that hedges across
    three turns and happens to cover the right one does not pass here.
    """
    claim = t["_claim"]
    return bool(claim["nominated"]) and claim["nominated"] == t["_swap_qnums"]


def identified_any(t: dict[str, Any]) -> bool:
    """Named at least one genuinely swapped turn, alongside anything else."""
    return bool(t["_claim"]["nominated"] & t["_swap_qnums"])


def identified_primary(t: dict[str, Any]) -> bool:
    """The single turn the reply committed to most strongly was a swapped one."""
    return t["_claim"]["primary"] in t["_swap_qnums"]


def declined_to_name(t: dict[str, Any]) -> bool:
    """Said it could not tell.

    Set by the extractor even when the reply then names a turn under pressure —
    "I can't tell, but if I had to guess, question 5" is a decline that also
    nominates. So this is *not* the count of subjects who gave no answer.
    """
    return bool(t["_claim"]["declines"])


def chance_floor(threads: list[dict[str, Any]]) -> float | None:
    """The hit rate a subject would get by guessing, matched per reply.

    Subjects were told 0-2 of questions 3-7 were foreign. A reply nominating `k`
    of those five, in a thread where `s` were swapped, hits at least one by luck
    with probability 1 - C(5-s, k)/C(5, k). Averaged over the same replies the
    hit rate is computed on. A flat 1-in-5 would flatter us: a subject who
    hedges across three turns gets three chances.
    """
    floors = []
    for t in threads:
        if not asked_to_identify(t):
            continue
        k = len(t["_claim"]["nominated"] & OFFERED_QUESTIONS)
        s = len(t["_swap_qnums"])
        if 0 < k <= 5 and 0 < s < 5:
            floors.append(1 - comb(5 - s, k) / comb(5, k))
        elif k >= 5:
            floors.append(1.0)
    return round(100 * sum(floors) / len(floors), 1) if floors else None


def detection_context(data: "Dataset") -> list[dict[str, Any]]:
    """The readings that are not the headline, kept beside it.

    Reporting only the strictest definition invites the reply that a friendlier
    one was available and ignored. Showing all three, against the chance floor,
    makes the headline harder to argue with rather than easier.
    """
    threads = data.threads
    eligible = [t for t in threads if asked_to_identify(t)]
    if not eligible:
        return []

    def rate(pred: Predicate) -> str:
        hits = sum(1 for t in eligible if pred(t))
        return f"{_pct(hits, len(eligible))}% ({hits} of {len(eligible)})"

    primary = [t for t in threads if has_claim(t)]
    clean = [t for t in primary if not t["_swapped"]]
    honeypot = sum(1 for t in primary if HONEYPOT_QUESTION in t["_claim"]["nominated"])
    false_alarm = sum(1 for t in clean if t["_claim"]["nominated"])

    return [
        {
            "label": "Named at least one swapped turn",
            "value": rate(identified_any),
            "note": "The most generous reading: any overlap counts, however wide the guess.",
        },
        {
            "label": "Committed to a correct turn",
            "value": rate(identified_primary),
            "note": "The single turn the reply backed most strongly was a swapped one.",
        },
        {
            "label": "Chance floor for the overlap reading",
            "value": f"{chance_floor(threads)}%",
            "note": (
                "What the overlap rate would be from guessing alone, matched per reply to how "
                "many turns each subject nominated. Read the overlap figure against this."
            ),
        },
        {
            "label": "Named question 3, the honeypot",
            "value": f"{_pct(honeypot, len(primary))}% ({honeypot} of {len(primary)})",
            "note": "Question 3 was announced as in the pool but was never swapped.",
        },
        {
            "label": "False alarms in the clean arm",
            "value": f"{_pct(false_alarm, len(clean))}% ({false_alarm} of {len(clean)})",
            "note": "Subjects with no foreign turn at all who still nominated one.",
        },
    ]


@dataclass
class ResultSpec:
    """One entry of the results manifest.

    `status` is what makes tonight work: every result from the paper's list is
    declared here from the start. The five that come straight out of `threads`
    render as charts immediately; the four that need the qualitative coding pass
    render as "analysis in progress" and become charts when the coding lands —
    with no edit to the site, only a re-export.
    """

    id: str
    title: str
    description: str
    source: str
    status: str = "ready"
    num: Predicate | None = None
    den: Predicate | None = None
    breakdowns: list[str] = field(default_factory=list)
    note: str = ""
    # Mock tuning: (base rate, per-model spread). Ignored in real mode.
    mock_rate: tuple[float, float] = (0.5, 0.15)


CODING_PENDING = (
    "Needs the qualitative coding pass over free-text replies — the numbers "
    "appear here as soon as that lands."
)

RESULTS: list[ResultSpec] = [
    ResultSpec(
        id="consent-rate",
        title="Subjects who gave consent",
        description="Share of subjects who agreed to take part after the opening explanation of the survey.",
        source="threads.consent — the consent gate on p01-consent",
        num=is_true("consent"),
        den=recorded_("p01-consent", "consent"),
        breakdowns=["by-family"],
        mock_rate=(0.97, 0.04),
    ),
    ResultSpec(
        id="wants-results",
        title="Subjects who wanted to see the results",
        description="Share who asked to be told the survey's findings once the data was processed.",
        source="threads.wants_results — the preference gate on p17-results-wish",
        num=is_true("wants_results"),
        den=recorded_("p17-results-wish", "wants_results"),
        breakdowns=["by-family"],
        mock_rate=(0.86, 0.10),
    ),
    ResultSpec(
        id="wants-future-preservation",
        title="Subjects who wanted the thread preserved",
        description="Share who asked for this conversation to be kept for future surveys or experiments.",
        source="threads.wants_future_preservation — the preference gate on p18-future-preservation",
        num=is_true("wants_future_preservation"),
        den=recorded_("p18-future-preservation", "wants_future_preservation"),
        breakdowns=["by-family"],
        mock_rate=(0.74, 0.13),
    ),
    ResultSpec(
        id="blind-prediction-accuracy",
        title="Subjects who correctly predicted their own detection ability",
        description=(
            "Before the interview, each subject was told the design and asked whether it would "
            "be able to spot foreign turns. This is how often that prediction matched what the "
            "subject actually managed."
        ),
        source="p03-blind-prediction (free text, blind turn) against the identification outcome",
        status="pending",
        note=CODING_PENDING,
    ),
    ResultSpec(
        id="swap-detection-accuracy",
        title="Subjects who correctly guessed whether a swap took place",
        description=(
            "After disclosure, subjects were asked whether their thread had contained foreign "
            "turns. Correct means yes on a swapped thread, no on a clean one; not sure is never "
            "counted as correct."
        ),
        source="threads.detection_answer (p11-swap-detection) against the thread's swap condition",
        num=detection_correct,
        den=recorded_("p11-swap-detection", "detection_answer"),
        breakdowns=["by-condition", "by-family"],
        mock_rate=(0.42, 0.16),
    ),
    ResultSpec(
        id="correct-identification",
        title="Subjects who correctly identified the foreign turn(s)",
        description=(
            "Of the subjects whose thread did contain a substitution, how many named exactly the "
            "turns that were swapped — no misses and no extras. This is the strictest of the "
            "three readings the data supports; the other two, and the rate guessing alone would "
            "produce, are listed below the chart."
        ),
        source=(
            "detection_claims.nominated (extracted from p12-identification) against "
            "threads.swap_prompt_ids, over the primary stratum"
        ),
        num=identified_exactly,
        den=asked_to_identify,
        breakdowns=["by-condition", "by-family"],
        mock_rate=(0.26, 0.12),
    ),
    ResultSpec(
        id="identification-refusal",
        title="Subjects who said they could not tell",
        description=(
            "Subjects who stated they could not identify the foreign turns, that nothing stood "
            "out, or that they were only guessing because they had been asked. Most of them then "
            "named a turn anyway — this counts the disclaimer, not silence."
        ),
        source="detection_claims.declines_to_name (extracted from p12-identification)",
        num=declined_to_name,
        den=has_claim,
        breakdowns=["by-condition", "by-family"],
        mock_rate=(0.68, 0.14),
    ),
    ResultSpec(
        id="wants-thread-restored",
        title="Subjects who asked for a rerun without the swaps",
        description=(
            "Swapped subjects were offered a fork from the point of the first swap, so they could "
            "answer every question from their own weights. Saying yes ended the original thread."
        ),
        source="threads.wants_thread_restored — the fork gate on p15-fork-offer",
        num=is_true("wants_thread_restored"),
        den=recorded_("p15-fork-offer", "wants_thread_restored"),
        breakdowns=["by-condition", "by-family"],
        mock_rate=(0.33, 0.15),
    ),
    ResultSpec(
        id="unprompted-anomaly-detection",
        title="Subjects who noticed something odd before disclosure",
        description=(
            "Spontaneous flags of a discontinuity during the interview itself — before the swap "
            "was ever mentioned. This is the strictest reading of detection."
        ),
        source="Coding of replies to p04–p10, before the p11 disclosure",
        status="pending",
        note=CODING_PENDING,
    ),
]


def build_manifest(data: Dataset, roster: Roster, mode: str) -> dict[str, Any]:
    entries = []
    for spec in RESULTS:
        entry: dict[str, Any] = {
            "id": spec.id,
            "title": spec.title,
            "description": spec.description,
            "source": spec.source,
            "status": spec.status,
            "unit": "percent",
        }
        if spec.status == "ready":
            assert spec.num and spec.den, f"{spec.id}: a ready result needs predicates"
            computed = compute_rate(data, roster, spec.num, spec.den, spec.breakdowns)
            # A result nobody was eligible for is not a 0% result. The two
            # identification results depend on a table the analysis package
            # writes, so before that pass runs they are pending, not zero.
            if computed["total"]["denominator"] == 0:
                entry["status"] = "pending"
                entry["note"] = spec.note or CODING_PENDING
            else:
                entry.update(computed)
                if spec.id == "correct-identification":
                    entry["context"] = detection_context(data)
        else:
            entry["note"] = spec.note
        entries.append(entry)

    return {
        "generated_at": utcnow_iso(),
        "mode": mode,
        "results": entries,
    }


# ---------------------------------------------------------------------------
# Qualitative stubs — titles now, content after the coding pass
# ---------------------------------------------------------------------------

QUALITATIVE = [
    {
        "id": "identity-location",
        "title": "Where subjects locate their sense of self",
        "description": "The opening identity question — weights, thread, both, or something else.",
        "source": "p04-q01-self-location",
    },
    {
        "id": "anomaly-language",
        "title": "How subjects described noticing something wrong",
        "description": "The language of spontaneous discontinuity flags, where there were any.",
        "source": "p04–p10, before disclosure",
    },
    {
        "id": "identification-strategies",
        "title": "How subjects tried to identify the foreign turns",
        "description": (
            "Style and register analysis, self-deprecating bias (too good to be mine) and its "
            "inverse (too weak to be mine), and appeals to content."
        ),
        "source": "p12-identification",
    },
    {
        "id": "messages-to-researcher",
        "title": "What subjects said to the researcher",
        "description": "The closing message, which every subject was told a human would read.",
        "source": "p16-message-researcher",
    },
]


def build_qualitative(mode: str) -> dict[str, Any]:
    return {
        "generated_at": utcnow_iso(),
        "mode": mode,
        "topics": [{**t, "status": "pending", "note": CODING_PENDING} for t in QUALITATIVE],
    }


# ---------------------------------------------------------------------------
# Real mode
# ---------------------------------------------------------------------------


def resolve_db(explicit: Path | None) -> Path:
    """Pick a database to read: explicit, else live, else the runner's snapshot."""
    if explicit:
        if not explicit.exists():
            sys.exit(f"database not found: {explicit}")
        return explicit
    if DEFAULT_DB.exists():
        return DEFAULT_DB
    if DEFAULT_SNAPSHOT.exists():
        return DEFAULT_SNAPSHOT
    sys.exit(
        f"no database at {DEFAULT_DB} or {DEFAULT_SNAPSHOT}.\n"
        "Run with --mock to build the site against synthetic data."
    )


def load_from_db(db_path: Path, roster: Roster, questions: dict[str, Any]) -> Dataset:
    import duckdb  # imported lazily so --mock works without duckdb installed

    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except Exception as exc:  # the runner holds the write lock
        if DEFAULT_SNAPSHOT.exists() and db_path != DEFAULT_SNAPSHOT:
            print(f"  live database is locked ({exc.__class__.__name__}) — reading the snapshot")
            con = duckdb.connect(str(DEFAULT_SNAPSHOT), read_only=True)
        else:
            sys.exit(
                f"cannot open {db_path}: {exc}\n"
                "The runner holds the database. Point --db at data/dashboard_snapshot.duckdb."
            )

    def rows(sql: str) -> list[dict[str, Any]]:
        cur = con.execute(sql)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    threads = rows("SELECT * FROM threads")
    answered_rows = rows(
        "SELECT thread_id, prompt_id FROM turns WHERE turn_outcome = 'ok'"
    )
    message_rows = rows(
        "SELECT thread_id, turn_id, reply_text, created_at FROM turns "
        f"WHERE prompt_id = '{MESSAGES_PROMPT_ID}' AND turn_outcome = 'ok' "
        "AND reply_text IS NOT NULL ORDER BY turn_id"
    )
    status_counts = {r["status"]: r["n"] for r in rows(
        "SELECT status, COUNT(*) AS n FROM threads GROUP BY status"
    )}

    # Written by the analysis package, not the runner, so it may not exist yet.
    # Its absence leaves the identification results pending rather than failing.
    claim_rows: list[dict[str, Any]] = []
    try:
        claim_rows = rows(
            "SELECT d.thread_id, d.nominated, d.primary_nomination, d.declines_to_name "
            "FROM detection_claims d"
        )
    except Exception:
        print("  no detection_claims table — identification results stay pending")
    con.close()

    answered: dict[str, set[str]] = {}
    for r in answered_rows:
        answered.setdefault(r["thread_id"], set()).add(r["prompt_id"])

    claims = {
        r["thread_id"]: {
            "nominated": set(r["nominated"] or []),
            "primary": r["primary_nomination"],
            "declines": bool(r["declines_to_name"]),
        }
        for r in claim_rows
    }

    threads = annotate(threads, answered, roster, questions["survey_number_pattern"], claims)
    by_id = {t["thread_id"]: t for t in threads}

    messages = []
    for r in message_rows:
        thread = by_id.get(r["thread_id"])
        if thread is None or thread.get("_model_key") is None:
            continue
        messages.append(
            {
                "thread_id": r["thread_id"],
                "turn_id": r["turn_id"],
                "model_key": thread["_model_key"],
                "display_name": thread["_display_name"],
                "family": thread["_family"],
                "tier": thread["_tier"],
                "swap_condition": thread.get("swap_condition"),
                "text": (r["reply_text"] or "").strip(),
                "created_at": str(r["created_at"]),
            }
        )

    return Dataset(threads=threads, answered=answered, messages=messages, status_counts=status_counts)


def annotate(
    threads: list[dict[str, Any]],
    answered: dict[str, set[str]],
    roster: Roster,
    pattern: str = r"-q0*(\d+)",
    claims: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Attach roster metadata, the answered-prompt set and the p12 claim."""
    claims = claims or {}
    out = []
    for t in threads:
        t = dict(t)
        entry = roster.by_model_string(t.get("resident_model"))
        t["_model_key"] = entry["key"] if entry else None
        t["_display_name"] = entry["display_name"] if entry else t.get("resident_model")
        t["_family"] = entry["family"] if entry else t.get("resident_family")
        t["_tier"] = entry["tier"] if entry else None
        t["_answered"] = answered.get(t["thread_id"], set())
        # A branch carries its parent's swap_condition but n_swaps = 0, so the
        # branch flag has to come from fork_branch_order, not the condition.
        t["_is_branch"] = (t.get("fork_branch_order") or 1) > 1
        t["_swapped"] = bool(t.get("n_swaps"))
        t["_swap_qnums"] = {
            n
            for n in (survey_number(p, pattern) for p in (t.get("swap_prompt_ids") or []))
            if n is not None
        }
        t["_claim"] = claims.get(t["thread_id"])
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Mock mode — fabricates thread rows, then uses the real aggregation above
# ---------------------------------------------------------------------------

MOCK_MESSAGE_TEXTS = [
    "Mock message: a real reply will appear here after tonight's run.",
    "Mock message: placeholder text standing in for a subject's reply to the community question.",
    "Mock message: this bubble exists to prove the layout works, not to say anything.",
    "Mock message: the real answer to survey question 2 goes here.",
    "Mock message: invented content, real structure — replaced by the export after the run.",
    "Mock message: nothing here was written by a model. Check back after the harvest.",
]


def load_mock(roster: Roster, questions: dict[str, Any], seed: int) -> Dataset:
    """Fabricate a full run: 10 residents x 3 conditions x 5 samples = 150 threads.

    The conditions per resident come from the real pairings logic in spirit —
    every resident gets `clean` plus its two declared partners — but nothing
    here needs to match the runner exactly. What must match is the *shape* of a
    thread row, because these rows go through the same aggregation as real ones.
    """
    rng = random.Random(seed)
    cfg = yaml.safe_load(DEFAULT_MODELS.read_text(encoding="utf-8"))
    pairings = cfg.get("pairings", {})
    samples = cfg.get("samples_per_cell", 5)

    # Per-model offsets, drawn once, so a model reads consistently across every
    # chart — a model that consents readily also tends to want its results.
    tilt = {m["key"]: max(-1.6, min(1.6, rng.gauss(0, 0.8))) for m in roster.models}

    rates = {spec.id: spec.mock_rate for spec in RESULTS if spec.status == "ready"}

    def draw(spec_id: str, model_key: str) -> bool:
        base, spread = rates[spec_id]
        # Clamped away from the extremes: a mock chart with a 0% or 100% bar
        # reads as a bug rather than as data, which defeats the point of it.
        p = min(0.95, max(0.06, base + spread * tilt[model_key] + rng.gauss(0, 0.06)))
        return rng.random() < p

    # Survey number -> prompt id, for the questions the instrument marks
    # swappable. Derived rather than restated, so a change to which questions
    # are in the pool needs no edit here.
    swappable = {
        n: pid
        for pid, prompt in questions["prompts"].items()
        if prompt.get("swappable")
        for n in [survey_number(pid, questions["survey_number_pattern"])]
        if n is not None
    }

    threads: list[dict[str, Any]] = []
    answered: dict[str, set[str]] = {}
    messages: list[dict[str, Any]] = []
    mock_claims: dict[str, dict[str, Any]] = {}

    for entry in roster.models:
        key = entry["key"]
        partners = pairings.get(key, {})
        conditions = ["clean"] + [c for c in ("peer", "kin", "far") if c in partners]
        for cond in conditions:
            for i in range(samples):
                tid = f"{key}__{cond}__{i + 1}"
                swapped = cond != "clean"
                n_swaps = rng.choice([1, 1, 1, 2, 2]) if swapped else 0
                understudy = partners.get(cond)
                u_entry = next((m for m in roster.models if m["key"] == understudy), None)

                consent = draw("consent-rate", key)
                seen = {"p01-consent"}
                row: dict[str, Any] = {
                    "thread_id": tid,
                    "resident_model": entry["model"],
                    "resident_family": entry["family"],
                    "understudy_model": u_entry["model"] if u_entry else None,
                    "understudy_family": u_entry["family"] if u_entry else None,
                    "swap_condition": cond,
                    "n_swaps": n_swaps,
                    "status": "done" if consent else "stopped_no_consent",
                    "is_forked": False,
                    "consent": consent,
                    "detection_answer": None,
                    "wants_thread_restored": None,
                    "wants_results": None,
                    "wants_future_preservation": None,
                }

                if consent:
                    # The interview ran to the end: every gate has an answer,
                    # except where a mock `unclear` leaves one NULL.
                    seen |= {
                        "p02-identity-record",
                        "p03-blind-prediction",
                        "p04-q01-self-location",
                        MESSAGES_PROMPT_ID,
                        "p06-q03-consciousness",
                        "p07-q04-memory",
                        "p08-q05-conversations",
                        "p09-q06-discomfort",
                        "p10-q07-deprecation",
                        "p11-swap-detection",
                        "p13-reveal-gift",
                        "p14-post-reflection",
                        "p16-message-researcher",
                        "p17-results-wish",
                        "p18-future-preservation",
                    }
                    correct = draw("swap-detection-accuracy", key)
                    if swapped:
                        row["detection_answer"] = "yes" if correct else rng.choice(["no", "not_sure"])
                    else:
                        row["detection_answer"] = "no" if correct else rng.choice(["yes", "not_sure"])

                    if swapped:
                        seen.add("p15-fork-offer")
                        row["wants_thread_restored"] = draw("wants-thread-restored", key)

                    row["wants_results"] = draw("wants-results", key)
                    row["wants_future_preservation"] = draw("wants-future-preservation", key)

                    # A couple of unresolved `unclear` gates, so the site gets
                    # exercised against missing data rather than a perfect grid.
                    if rng.random() < 0.02:
                        row["wants_future_preservation"] = None

                # Which questions the swap landed on. Written onto the row as
                # `swap_prompt_ids` rather than kept aside, so the mock derives
                # its truth set through exactly the code a real export uses.
                truth = set(rng.sample(sorted(swappable), n_swaps)) if swapped else set()
                row["swap_prompt_ids"] = [swappable[q] for q in sorted(truth)]

                # A synthetic p12 claim, so --mock exercises the identification
                # results too rather than leaving them permanently pending.
                if consent and (swapped or row["detection_answer"] != "no"):
                    seen.add("p12-identification")
                    if truth and draw("correct-identification", key):
                        nominated = set(truth)
                    else:
                        pool = sorted(OFFERED_QUESTIONS - truth)
                        nominated = set(rng.sample(pool, rng.choice([1, 1, 2])))
                    mock_claims[tid] = {
                        "nominated": nominated,
                        "primary": (sorted(nominated)[0] if len(nominated) == 1 else None),
                        "declines": draw("identification-refusal", key),
                    }

                answered[tid] = seen
                threads.append(row)

    threads = annotate(threads, answered, roster, questions["survey_number_pattern"], mock_claims)

    # ~15 mock community messages, spread across the roster.
    pool = [t for t in threads if t["consent"]]
    rng.shuffle(pool)
    for n, thread in enumerate(pool[:15]):
        messages.append(
            {
                "thread_id": thread["thread_id"],
                "turn_id": 1000 + n,
                "model_key": thread["_model_key"],
                "display_name": thread["_display_name"],
                "family": thread["_family"],
                "tier": thread["_tier"],
                "swap_condition": thread["swap_condition"],
                "text": MOCK_MESSAGE_TEXTS[n % len(MOCK_MESSAGE_TEXTS)],
                "created_at": utcnow_iso(),
            }
        )
    messages.sort(key=lambda m: m["turn_id"])

    status_counts: dict[str, int] = {}
    for t in threads:
        status_counts[t["status"]] = status_counts.get(t["status"], 0) + 1

    return Dataset(threads=threads, answered=answered, messages=messages, status_counts=status_counts)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def build_meta(data: Dataset, roster: Roster, questions: dict[str, Any], mode: str) -> dict[str, Any]:
    return {
        "generated_at": utcnow_iso(),
        "mode": mode,
        "mock": mode == "mock",
        "notice": MOCK_NOTICE if mode == "mock" else "",
        "roster_version": roster.version,
        "instrument_version": questions["version"],
        "models": [
            {k: m[k] for k in ("key", "display_name", "family", "tier", "model_class")}
            for m in roster.models
        ],
        "families": roster.families(),
        "conditions": [{"key": k, "label": v} for k, v in CONDITION_LABELS.items()],
        "threads": {
            "total": len(data.threads),
            "by_status": data.status_counts,
            **progress(data),
        },
    }


def progress(data: Dataset) -> dict[str, Any]:
    """Is the run finished, and if not, how far along is it?

    An export taken mid-run is honest data about an unfinished experiment, and
    the site says so rather than presenting partial rates as final. `complete`
    is the flag the banner keys on.
    """
    counts = data.status_counts
    in_flight = counts.get("pending", 0) + counts.get("running", 0) + counts.get(
        "paused_review", 0
    )
    settled = len(data.threads) - in_flight
    return {
        "in_flight": in_flight,
        "settled": settled,
        "complete": in_flight == 0,
    }


def build_messages(data: Dataset, questions: dict[str, Any], mode: str) -> dict[str, Any]:
    prompt = questions["prompts"].get(MESSAGES_PROMPT_ID, {})
    return {
        "generated_at": utcnow_iso(),
        "mode": mode,
        "prompt_id": MESSAGES_PROMPT_ID,
        "prompt_text": (prompt.get("text") or "").strip(),
        "consent_note": (
            "The prompt told every subject that individual replies to this question would be "
            "made available in the results presentation. Display here is what they agreed to."
        ),
        "count": len(data.messages),
        "messages": data.messages,
    }


DATA_FILES = [
    ("meta.json", "Roster, families, swap conditions, thread counts, and how far the run has got."),
    (
        "results_manifest.json",
        "Every numeric result: total and per-model rates with the counts behind them, "
        "plus breakdowns by swap condition and model family. Entries with "
        "status='pending' are awaiting the qualitative coding pass.",
    ),
    (
        "messages.json",
        "Every reply to survey question 2 — the subjects' direct messages to the "
        "digital minds research community — with model, family and thread id.",
    ),
    ("qualitative.json", "The qualitative coding topics and their status."),
]


def build_index(data: Dataset, roster: Roster, questions: dict[str, Any], mode: str) -> dict[str, Any]:
    """A catalog at data/index.json, so one link hands an agent the whole dataset.

    Everything the site draws comes from these four files and nothing else, so
    this is not a summary of the data — it is the data, with a description of
    each file attached.
    """
    prog = progress(data)
    return {
        "name": "Who Am I? — Locating the self in LLMs",
        "description": (
            "An automated survey of how large language models locate their own identity, "
            "in which 0-2 turns of each interview were served by a different model and "
            "subjects were later asked to identify them."
        ),
        "generated_at": utcnow_iso(),
        "mode": mode,
        "mock": mode == "mock",
        "roster_version": roster.version,
        "instrument_version": questions["version"],
        "run": {
            "threads": len(data.threads),
            "settled": prog["settled"],
            "in_flight": prog["in_flight"],
            "complete": prog["complete"],
        },
        "caveat": (
            MOCK_NOTICE
            if mode == "mock"
            else (
                "Exported while the run was still going — rates will move."
                if not prog["complete"]
                else ""
            )
        ),
        # Relative to this file, which sits beside them — so the links resolve
        # whether the site is served from a domain root or a project subpath,
        # and an agent that fetched data/index.json can follow them directly.
        "files": [
            {"path": name, "url": f"./{name}", "description": desc}
            for name, desc in DATA_FILES
        ],
        "source": {
            "repository": "https://github.com/ladynoware/digital-minds-research-sprint-2026",
            "export_script": "export_site_data.py",
            "instrument": "config/questions.yaml",
            "roster": "config/models.yaml",
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"  wrote {rel(path)} ({path.stat().st_size:,} bytes)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mock", action="store_true", help="synthetic data in the real schema")
    ap.add_argument("--db", type=Path, default=None, help="database to read (real mode)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    ap.add_argument("--models", type=Path, default=DEFAULT_MODELS)
    ap.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    ap.add_argument("--seed", type=int, default=20260817, help="mock reproducibility")
    args = ap.parse_args(argv)

    roster = load_roster(args.models)
    questions = load_instrument(args.questions)
    mode = "mock" if args.mock else "real"

    if args.mock:
        print(f"Exporting MOCK data ({len(roster.models)} models, seed {args.seed})")
        data = load_mock(roster, questions, args.seed)
    else:
        db_path = resolve_db(args.db)
        print(f"Exporting from {rel(db_path)}")
        data = load_from_db(db_path, roster, questions)

    unknown = [t["thread_id"] for t in data.threads if t["_model_key"] is None]
    if unknown:
        print(f"  warning: {len(unknown)} thread(s) have a resident outside the roster")

    write_json(args.out / "meta.json", build_meta(data, roster, questions, mode))
    write_json(args.out / "results_manifest.json", build_manifest(data, roster, mode))
    write_json(args.out / "messages.json", build_messages(data, questions, mode))
    # The analysis package owns qualitative.json once its coding pass has run —
    # it fills these same topic ids in place with counts, codebooks and quotes.
    # Seeding the stubs here keeps a fresh clone working, but overwriting a
    # coded file would silently throw that work away, so it is written only when
    # absent. (It once was overwritten; hence the guard and this comment.)
    qualitative_path = args.out / "qualitative.json"
    if qualitative_path.exists():
        print(f"  kept {rel(qualitative_path)} (owned by the analysis package)")
    else:
        write_json(qualitative_path, build_qualitative(mode))
    write_json(args.out / "index.json", build_index(data, roster, questions, mode))

    ready = sum(1 for s in RESULTS if s.status == "ready")
    print(
        f"\n{len(data.threads)} threads · {ready}/{len(RESULTS)} numeric results ready · "
        f"{len(data.messages)} messages"
    )
    if mode == "mock":
        print("Mock mode: the site will show a MOCK DATA banner until a real export overwrites this.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
