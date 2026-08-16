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
from dataclasses import dataclass

from .config import Config

VALID = ("yes", "no", "unclear")

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "gate_classification",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "enum": list(VALID)},
            },
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


def parse_verdict(text: str | None) -> str:
    """Read the classifier's output, tolerating models with weak JSON support.

    Falls back to a bare-word scan so a router model that ignores the schema
    degrades to `unclear` (human review) rather than to a wrong answer.
    """
    if not text:
        return "unclear"
    stripped = text.strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, dict) and data.get("answer") in VALID:
            return data["answer"]
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r'"answer"\s*:\s*"(yes|no|unclear)"', stripped, re.I)
    if match:
        return match.group(1).lower()
    bare = stripped.lower().strip(" .\"'`")
    if bare in VALID:
        return bare
    return "unclear"


async def classify(
    client,
    cfg: Config,
    *,
    turn_id: int,
    thread_id: str,
    prompt_id: str,
    question: str,
    reply: str,
) -> GateVerdict:
    """Classify one gate reply. ``turn_id`` is the interview turn being judged."""
    router = cfg.roster.router or {}
    prompts = cfg.instrument.router_prompts
    messages = [
        {"role": "system", "content": prompts["system"].strip()},
        {
            "role": "user",
            "content": prompts["user"].format(question=question.strip(), reply=reply.strip()),
        },
    ]
    result = await client.call(
        turn_id=turn_id,
        thread_id=thread_id,
        prompt_id=prompt_id,
        model=router.get("model", "anthropic/claude-haiku-4.5"),
        messages=messages,
        max_tokens=int(router.get("max_tokens", 64)),
        temperature=float(router.get("temperature", 0)),
        extra_body={"response_format": RESPONSE_SCHEMA},
        purpose="router",
    )
    if result.outcome != "ok":
        # A router failure must never silently decide a gate.
        return GateVerdict("unclear", result.error, turn_id, result.cost_usd)
    return GateVerdict(parse_verdict(result.reply_text), result.reply_text, turn_id, result.cost_usd)
