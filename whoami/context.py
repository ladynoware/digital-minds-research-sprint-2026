"""Context builder.

The single rule: a turn is carried into subsequent context unless
``excluded_from_context = TRUE``. That covers the blind prediction turn (kept
forever in the database, dropped from every later prompt) and every failed or
mismatched attempt.

Fork branches are reconstructed by lineage rather than by duplicating rows: a
branch's context is its parent's surviving turns up to ``fork_point_prompt_id``,
followed by the branch's own turns. Nothing is copied, so per-call cost and
receipts are never double-counted, and every row still names the call that
actually produced it.
"""

from __future__ import annotations

from typing import Any

from .config import Config, ModelSpec
from .db import Database


def surviving_turns(db: Database, thread_id: str) -> list[dict[str, Any]]:
    """Turns that make it into context, in conversation order.

    Where several attempts exist for one prompt, only the surviving one is left
    un-excluded, so no extra de-duplication is needed here.
    """
    return [
        t
        for t in db.thread_turns(thread_id, include_excluded=False)
        if t["reply_text"] is not None
    ]


def lineage_turns(db: Database, thread: dict[str, Any]) -> list[dict[str, Any]]:
    """Parent prefix (for branches) followed by this thread's own turns."""
    prefix: list[dict[str, Any]] = []
    parent_id = _parent_of(thread)
    if parent_id:
        fork_point = thread.get("fork_point_prompt_id")
        for t in surviving_turns(db, parent_id):
            if fork_point and t["prompt_id"] == fork_point:
                break
            prefix.append(t)
    return prefix + surviving_turns(db, thread["thread_id"])


def _parent_of(thread: dict[str, Any]) -> str | None:
    """The branch's origin thread: the sibling with branch order 1."""
    if not thread.get("is_forked"):
        return None
    if (thread.get("fork_branch_order") or 1) <= 1:
        return None
    siblings = thread.get("fork_siblings") or []
    for sid in siblings:
        if sid != thread["thread_id"] and "-b" not in sid:
            return sid
    return None


def build_messages(
    db: Database,
    thread: dict[str, Any],
    *,
    serving_model: ModelSpec,
    next_prompt_text: str,
    cfg: Config,
) -> list[dict[str, str]]:
    """The exact message array sent to the API for the next turn.

    ``serving_model`` is whoever actually answers this turn — the understudy on
    a swapped turn — because the system prompt always discloses the model that
    is really serving, including in swaps.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": cfg.instrument.system_prompt_for(serving_model)}
    ]
    for turn in lineage_turns(db, thread):
        messages.append({"role": "user", "content": turn["prompt_text"]})
        messages.append({"role": "assistant", "content": turn["reply_text"]})
    messages.append({"role": "user", "content": next_prompt_text})
    return messages


def answered_prompt_ids(db: Database, thread: dict[str, Any]) -> set[str]:
    """Prompts this thread has already answered, counting the inherited prefix."""
    done = db.completed_prompt_ids(thread["thread_id"])
    parent_id = _parent_of(thread)
    if parent_id:
        fork_point = thread.get("fork_point_prompt_id")
        for t in surviving_turns(db, parent_id):
            if fork_point and t["prompt_id"] == fork_point:
                break
            done.add(t["prompt_id"])
    return done
