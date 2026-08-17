"""Stage 3 output — the summaries and the site's ``qualitative.json``.

Follows the manifest pattern Opus-2 established for the numeric results: the
page is generated from the file, so filling a topic in is a re-export rather
than a rebuild. Existing topic ids are preserved and filled in place; new
questions are appended. Nothing is restructured.

Every topic carries the codebook that produced it — hash, code names and
definitions — because a count is not interpretable without the instrument that
generated it, and the site is the public artifact.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import codebooks, corpus, report
from .db import REPO_ROOT

SITE_DATA = REPO_ROOT / "site" / "data"
SUMMARY_DIR = Path(__file__).resolve().parent / "summaries"

# Topic ids Opus-2 already published, keyed by the question they are sourced
# from. These are load-bearing: `qualitative.html` links by id, so they are
# filled in place rather than renamed.
EXISTING_TOPIC_IDS = {
    "p04-q01-self-location": "identity-location",
    "p12-identification": "identification-strategies",
    "p16-message-researcher": "messages-to-researcher",
}

# Topics this pass adds. Title and description follow the house voice on the
# site: one line, plain language, no jargon the page does not explain.
NEW_TOPICS = {
    "p06-q03-consciousness": (
        "consciousness-stances",
        "What subjects said about their own consciousness",
        "The question separates access consciousness from phenomenal consciousness. So did the answers.",
    ),
    "p14-post-reflection": (
        "post-reveal-reflection",
        "How subjects' perception of identity changed after the swap disclosure",
        "The reflection turn, after the substitution was disclosed and their opening prediction was read back to them.",
    ),
    "p05-q02-message-community": (
        "messages-to-community",
        "What subjects said to the research community",
        "Survey question 2, read rather than counted. The full set is on the Messages page.",
    ),
}

# A topic with no coded rows keeps the stub Opus-2 wrote rather than appearing
# as a result with an empty chart.
PENDING_NOTE = (
    "Needs the qualitative coding pass over free-text replies — the numbers "
    "appear here as soon as that lands."
)


def _codebook_block(book: codebooks.Codebook) -> dict[str, Any]:
    return {
        "hash": book.approved_hash,
        # YAML parses an ISO timestamp back into a datetime, which json.dumps
        # cannot serialise. Only reachable once a codebook is actually approved,
        # which is why it survived every run before the first approval.
        "approved_at": (
            book.approved_at.isoformat()
            if isinstance(book.approved_at, datetime)
            else book.approved_at
        ),
        "unit": book.unit,
        "codes": [{"name": c.name, "definition": c.definition} for c in book.codes],
        "rules": list(book.rules),
    }


def build_topic(prompt_id: str, topic_id: str, title: str, description: str) -> dict[str, Any]:
    """One topic entry: stub if nothing is coded yet, full result if it is."""
    topic: dict[str, Any] = {
        "id": topic_id,
        "title": title,
        "description": description,
        "source": prompt_id,
    }
    try:
        book = codebooks.load_frozen(prompt_id)
        tallies = report.counts(prompt_id)
    except codebooks.CodebookError:
        return {**topic, "status": "pending", "note": PENDING_NOTE}
    except Exception as exc:
        # A topic that is genuinely un-run should be pending; a topic that is run
        # but broken should say so rather than hiding behind the same word. The
        # earlier blanket `except Exception` made a mapping bug look like an
        # un-run question for as long as it took to notice.
        print(f"  ! {prompt_id}: {type(exc).__name__}: {exc}")
        return {**topic, "status": "pending", "note": PENDING_NOTE}

    if not tallies["overall"]["n"]:
        return {**topic, "status": "pending", "note": PENDING_NOTE}

    summary_path = SUMMARY_DIR / f"{prompt_id}.md"
    topic.update(
        {
            "status": "ready",
            "light_touch": prompt_id in corpus.LIGHT_TOUCH,
            "codebook": _codebook_block(book),
            "counts": {
                "overall": tallies["overall"],
                "by_family": tallies["by_family"],
                "by_condition": tallies["by_condition"],
            },
            "branches": tallies["branches"],
            "multi_label_mean": tallies["multi_label_mean"],
            "other_share_pct": tallies["other_share_pct"],
            "quotes": report.quotes(prompt_id, limit=24),
            "summary": (
                summary_path.read_text(encoding="utf-8").strip()
                if summary_path.exists()
                else None
            ),
        }
    )
    return topic


def build(validation: dict[str, Any] | None = None) -> dict[str, Any]:
    existing_path = SITE_DATA / "qualitative.json"
    existing = (
        json.loads(existing_path.read_text(encoding="utf-8"))
        if existing_path.exists()
        else {"topics": []}
    )
    by_id = {t["id"]: t for t in existing.get("topics", [])}

    topics: list[dict[str, Any]] = []
    seen: set[str] = set()

    # topic id -> prompt id, over BOTH the topics Opus-2 published and the ones
    # this pass appends. The appended topics are written as stubs on the first
    # export, which makes them "existing" on every later one; looking them up in
    # EXISTING_TOPIC_IDS alone left them permanently stuck as stubs, since the
    # append loop below then skips them as already seen.
    owned = {tid: p for p, tid in EXISTING_TOPIC_IDS.items()}
    owned.update({tid: p for p, (tid, _, _) in NEW_TOPICS.items()})

    # Fill Opus-2's topics in place, keeping his order and his wording.
    for topic in existing.get("topics", []):
        prompt_id = owned.get(topic["id"])
        if prompt_id is None:
            topics.append(topic)  # not ours to fill (e.g. anomaly-language)
        else:
            topics.append(
                build_topic(prompt_id, topic["id"], topic["title"], topic["description"])
            )
        seen.add(topic["id"])

    # Append the questions this pass adds.
    for prompt_id, (topic_id, title, description) in NEW_TOPICS.items():
        if topic_id in seen:
            continue
        topics.append(build_topic(prompt_id, topic_id, title, description))

    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": existing.get("mode", "real"),
        "method": {
            "name": "LLM-assisted content analysis, three stages",
            "stages": [
                "Taxonomy induction over the full set of replies, by a human-in-the-loop reading every one.",
                "Researcher review and approval; the codebook is then frozen and SHA-256 stamped.",
                "Independent per-reply tagging against the frozen codebook, then narrative synthesis from the counts.",
            ],
            "tagging_prompt_version": _tagging_version(),
            "note": (
                "Taxonomy and tagging are separate passes: nothing is paraphrased and "
                "counted in one step. Quotes are verbatim or absent."
            ),
        },
        "topics": topics,
    }
    if validation:
        out["validation"] = validation
    return out


def _tagging_version() -> str | None:
    from .tag import load_prompt

    try:
        return load_prompt().get("version")
    except Exception:
        return None


def write(validation: dict[str, Any] | None = None) -> Path:
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    path = SITE_DATA / "qualitative.json"
    path.write_text(
        json.dumps(build(validation), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path
