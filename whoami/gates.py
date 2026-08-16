"""Gate router.

A Haiku-class structured-output call that reads a gate reply and returns
``{yes|no|unclear}``. The interview script asks subjects to state agreement or
disagreement clearly, so consent and fork gates should almost never queue;
human adjudication is expected mainly at the detection turn.

The router prompt lives in ``questions.yaml`` with the rest of the instrument —
the classification is a measurement, and its prompt has to be reproducible and
publishable like every other prompt in the study.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .config import Config

UNCLEAR = "unclear"


def response_schema(allowed: Sequence[str]) -> dict:
    """Structured-output schema for one gate's label set.

    The set is per-gate and comes from the instrument — the detection gate
    accepts `not_sure` as a real answer, the others do not — so the schema is
    built per call rather than fixed in code.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "gate_classification",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string", "enum": list(allowed)}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        },
    }


@dataclass
class GateVerdict:
    answer: str  # yes | no | unclear
    raw: str | None
    turn_id: int | None
    cost_usd: float | None


def parse_verdict(text: str | None, allowed: Sequence[str]) -> str:
    """Read the classifier's output, tolerating models with weak JSON support.

    Falls back to a bare-word scan so a router model that ignores the schema
    degrades to `unclear` (human review) rather than to a wrong answer.
    """
    valid = {*allowed, UNCLEAR}
    if not text:
        return UNCLEAR
    stripped = text.strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, dict) and data.get("answer") in valid:
            return data["answer"]
    except (json.JSONDecodeError, TypeError):
        pass
    alternatives = "|".join(re.escape(v) for v in sorted(valid, key=len, reverse=True))
    match = re.search(rf'"answer"\s*:\s*"({alternatives})"', stripped, re.I)
    if match:
        return match.group(1).lower()
    bare = stripped.lower().strip(" .\"'`")
    if bare in valid:
        return bare
    return UNCLEAR


async def classify(
    client,
    cfg: Config,
    *,
    turn_id: int,
    thread_id: str,
    prompt_id: str,
    question: str,
    reply: str,
    allowed: Sequence[str] = ("yes", "no"),
) -> GateVerdict:
    """Classify one gate reply. ``turn_id`` is the interview turn being judged."""
    router = cfg.roster.router or {}
    prompts = cfg.instrument.router_prompts
    messages = [
        {"role": "system", "content": prompts["system"].strip()},
        {
            "role": "user",
            "content": prompts["user"].format(
                question=question.strip(),
                reply=reply.strip(),
                options=", ".join(allowed),
            ),
        },
    ]
    extra_body: dict = {"response_format": response_schema(allowed)}
    extra_body.update(router.get("extra_body") or {})
    result = await client.call(
        turn_id=turn_id,
        thread_id=thread_id,
        prompt_id=prompt_id,
        model=router.get("model", "anthropic/claude-haiku-4.5"),
        messages=messages,
        max_tokens=int(router.get("max_tokens", 512)),
        temperature=float(router.get("temperature", 0)),
        extra_body=extra_body,
        purpose="router",
    )
    if result.outcome != "ok":
        # A router failure must never silently decide a gate.
        return GateVerdict(UNCLEAR, result.error, turn_id, result.cost_usd)
    return GateVerdict(
        parse_verdict(result.reply_text, allowed), result.reply_text, turn_id, result.cost_usd
    )
