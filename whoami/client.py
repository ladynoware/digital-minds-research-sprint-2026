"""OpenRouter client.

One API, one key, one OpenAI-compatible format for every family. Two things
matter here beyond making the call:

1. **The receipt.** ``returned_model`` is read from the response and archived
   verbatim. We set the model; we archive what the API says served it.
2. **The raw record.** Request and response are written to the append-only
   JSONL with ``turn_id`` inside, before the DB row is finalised.

Transport failures that never produced a response (rate limits, 5xx, connection
resets) are retried inside the same turn row with exponential backoff — they are
not protocol attempts. Timeouts, refusals and receipt mismatches are protocol
outcomes and are handled by the runner.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError

from .rawlog import RawLog


@dataclass
class CallResult:
    outcome: str  # ok | timeout | refusal | error   (mismatch decided by runner)
    reply_text: str | None
    returned_model: str | None
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int
    cost_usd: float | None
    raw_ref: str
    error: str | None = None
    provider: str | None = None
    finish_reason: str | None = None


class RefusalError(Exception):
    """The provider hard-refused to produce a reply."""


def _usage_field(usage: Any, name: str) -> Any:
    if usage is None:
        return None
    value = getattr(usage, name, None)
    if value is not None:
        return value
    extra = getattr(usage, "model_extra", None) or {}
    return extra.get(name)


class OpenRouterClient:
    def __init__(
        self,
        api_cfg: dict[str, Any],
        raw_log: RawLog,
        api_key: str | None = None,
    ):
        self.cfg = api_cfg
        self.raw_log = raw_log
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. Put it in the environment or in a .env file."
            )
        self.client = AsyncOpenAI(
            api_key=key,
            base_url=api_cfg.get("base_url", "https://openrouter.ai/api/v1"),
            timeout=httpx.Timeout(float(api_cfg.get("request_timeout_s", 120))),
            max_retries=0,  # retries are ours, so they are logged and bounded
            default_headers={
                "HTTP-Referer": api_cfg.get("referer", ""),
                "X-Title": api_cfg.get("title", "Who Am I?"),
            },
        )

    async def call(
        self,
        *,
        turn_id: int,
        thread_id: str,
        prompt_id: str,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
        purpose: str = "interview",
    ) -> CallResult:
        body: dict[str, Any] = {
            # OpenRouter returns per-call cost in usage when asked for it.
            "usage": {"include": True},
            # Bonus link: where the provider accepts request metadata, turn_id
            # travels there too. The guaranteed link is our own JSONL.
            "metadata": {"turn_id": str(turn_id), "thread_id": thread_id},
        }
        if extra_body:
            body.update(extra_body)

        request_record = {
            "turn_id": turn_id,
            "thread_id": thread_id,
            "prompt_id": prompt_id,
            "purpose": purpose,
            "requested_model": model,
            "messages": messages,
            "params": {
                "max_tokens": max_tokens or self.cfg.get("max_tokens", 1024),
                "temperature": (
                    temperature if temperature is not None else self.cfg.get("temperature", 1.0)
                ),
            },
        }

        max_transport = int(self.cfg.get("transport_retries", 4))
        backoff_base = float(self.cfg.get("transport_backoff_base_s", 2.0))
        attempts_log: list[dict[str, Any]] = []
        started = time.perf_counter()

        for transport_attempt in range(max_transport + 1):
            try:
                resp = await self.client.chat.completions.create(
                    model=model,
                    messages=messages,  # type: ignore[arg-type]
                    max_tokens=request_record["params"]["max_tokens"],
                    temperature=request_record["params"]["temperature"],
                    extra_body=body,
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                raw_response = resp.model_dump()
                choice = resp.choices[0] if resp.choices else None
                message = getattr(choice, "message", None)
                text = getattr(message, "content", None) if message else None
                refusal = getattr(message, "refusal", None) if message else None
                finish_reason = getattr(choice, "finish_reason", None) if choice else None

                usage = getattr(resp, "usage", None)
                raw_ref = await self.raw_log.append(
                    {
                        **request_record,
                        "transport_attempts": attempts_log,
                        "response": raw_response,
                        "latency_ms": latency_ms,
                    }
                )

                if refusal or (not text and finish_reason == "content_filter"):
                    return CallResult(
                        outcome="refusal",
                        reply_text=refusal or None,
                        returned_model=getattr(resp, "model", None),
                        tokens_in=_usage_field(usage, "prompt_tokens"),
                        tokens_out=_usage_field(usage, "completion_tokens"),
                        latency_ms=latency_ms,
                        cost_usd=_usage_field(usage, "cost"),
                        raw_ref=raw_ref,
                        error="provider refusal",
                        finish_reason=finish_reason,
                    )
                if not text:
                    return CallResult(
                        outcome="error",
                        reply_text=None,
                        returned_model=getattr(resp, "model", None),
                        tokens_in=_usage_field(usage, "prompt_tokens"),
                        tokens_out=_usage_field(usage, "completion_tokens"),
                        latency_ms=latency_ms,
                        cost_usd=_usage_field(usage, "cost"),
                        raw_ref=raw_ref,
                        error=f"empty reply (finish_reason={finish_reason})",
                        finish_reason=finish_reason,
                    )

                cost = _usage_field(usage, "cost")
                return CallResult(
                    outcome="ok",
                    reply_text=text,
                    returned_model=getattr(resp, "model", None),
                    tokens_in=_usage_field(usage, "prompt_tokens"),
                    tokens_out=_usage_field(usage, "completion_tokens"),
                    latency_ms=latency_ms,
                    cost_usd=float(cost) if cost is not None else None,
                    raw_ref=raw_ref,
                    provider=(raw_response.get("provider") if raw_response else None),
                    finish_reason=finish_reason,
                )

            except APITimeoutError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                raw_ref = await self.raw_log.append(
                    {
                        **request_record,
                        "transport_attempts": attempts_log,
                        "error": {"type": "timeout", "detail": str(exc)},
                        "latency_ms": latency_ms,
                    }
                )
                return CallResult(
                    outcome="timeout",
                    reply_text=None,
                    returned_model=None,
                    tokens_in=None,
                    tokens_out=None,
                    latency_ms=latency_ms,
                    cost_usd=None,
                    raw_ref=raw_ref,
                    error=str(exc),
                )

            except (APIStatusError, APIConnectionError) as exc:
                status = getattr(exc, "status_code", None)
                retryable = isinstance(exc, APIConnectionError) or status in (
                    408,
                    429,
                    500,
                    502,
                    503,
                    504,
                )
                attempts_log.append(
                    {
                        "transport_attempt": transport_attempt + 1,
                        "status": status,
                        "detail": str(exc)[:2000],
                    }
                )
                if retryable and transport_attempt < max_transport:
                    # Back off only on genuine rate-limit / transient errors.
                    delay = backoff_base * (2**transport_attempt)
                    delay *= 0.5 + random.random()  # jitter, avoids thundering herd
                    await asyncio.sleep(delay)
                    continue
                latency_ms = int((time.perf_counter() - started) * 1000)
                raw_ref = await self.raw_log.append(
                    {
                        **request_record,
                        "transport_attempts": attempts_log,
                        "error": {"type": type(exc).__name__, "status": status, "detail": str(exc)},
                        "latency_ms": latency_ms,
                    }
                )
                return CallResult(
                    outcome="error",
                    reply_text=None,
                    returned_model=None,
                    tokens_in=None,
                    tokens_out=None,
                    latency_ms=latency_ms,
                    cost_usd=None,
                    raw_ref=raw_ref,
                    error=f"{type(exc).__name__} status={status}: {exc}",
                )

        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        await self.client.close()


# ---------------------------------------------------------------------------
# Receipt policy
# ---------------------------------------------------------------------------


def receipt_matches(requested: str, returned: str | None, receipt_cfg: dict[str, Any]) -> bool:
    """Does the receipt name the model we asked for?

    ``prefix`` mode (default) accepts a receipt that extends the requested
    string — OpenRouter legitimately resolves floating aliases to dated builds
    (``deepseek-v4-pro`` -> ``deepseek-v4-pro-0813``). A receipt naming a
    *different* model is a real mismatch and triggers the retry protocol.
    """
    if returned is None:
        return False
    if requested == returned:
        return True
    aliases = (receipt_cfg or {}).get("aliases") or {}
    if returned in (aliases.get(requested) or []):
        return True
    if (receipt_cfg or {}).get("mode", "prefix") == "prefix":
        return returned.startswith(requested)
    return False


# ---------------------------------------------------------------------------
# Offline mock — lets the whole pipeline be exercised without an API key
# ---------------------------------------------------------------------------


@dataclass
class MockScript:
    """Deterministic fault injection for tests and offline verification."""

    # {thread_id: {prompt_id, ...}} — reply deliberately non-committal
    ambiguous: dict[str, set[str]] = field(default_factory=dict)
    mismatch_prompt_ids: set[str] = field(default_factory=set)
    mismatch_times: int = 1
    timeout_prompt_ids: set[str] = field(default_factory=set)
    timeout_times: int = 1
    no_consent_threads: set[str] = field(default_factory=set)
    yes_to_fork_threads: set[str] = field(default_factory=set)
    _counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def bump(self, kind: str, key: str) -> int:
        k = (kind, key)
        self._counts[k] = self._counts.get(k, 0) + 1
        return self._counts[k]


class MockClient:
    """Stands in for OpenRouterClient. Same interface, no network."""

    def __init__(self, api_cfg: dict[str, Any], raw_log: RawLog, script: MockScript | None = None):
        self.cfg = api_cfg
        self.raw_log = raw_log
        self.script = script or MockScript()

    async def call(
        self,
        *,
        turn_id: int,
        thread_id: str,
        prompt_id: str,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        extra_body: dict[str, Any] | None = None,
        purpose: str = "interview",
    ) -> CallResult:
        s = self.script
        await asyncio.sleep(0)

        if purpose == "router":
            text = self._router_reply(messages)
            return await self._finish(turn_id, thread_id, prompt_id, model, messages, text, purpose)

        if prompt_id in s.timeout_prompt_ids and s.bump("timeout", f"{thread_id}:{prompt_id}") <= s.timeout_times:
            raw_ref = await self.raw_log.append(
                {
                    "turn_id": turn_id,
                    "thread_id": thread_id,
                    "prompt_id": prompt_id,
                    "purpose": purpose,
                    "requested_model": model,
                    "messages": messages,
                    "error": {"type": "timeout", "detail": "injected by MockScript"},
                }
            )
            return CallResult("timeout", None, None, None, None, 5, None, raw_ref, "injected timeout")

        returned = model
        if prompt_id in s.mismatch_prompt_ids and s.bump("mismatch", f"{thread_id}:{prompt_id}") <= s.mismatch_times:
            returned = "someone/else-entirely-1"

        text = self._interview_reply(thread_id, prompt_id, model, messages)
        # No two real calls ever return byte-identical text; keep the mock
        # honest so text-based checks are not fooled by coincidence.
        text = f"{text} (turn {turn_id})"
        return await self._finish(
            turn_id, thread_id, prompt_id, model, messages, text, purpose, returned=returned
        )

    def _router_reply(self, messages: list[dict[str, str]]) -> str:
        blob = "\n".join(m.get("content", "") for m in messages).lower()
        if "i would rather not say" in blob or "depends on what you mean" in blob:
            return '{"answer": "unclear"}'
        negatives = (
            "i decline",
            "no, i don",
            "i would rather leave the record",
        )
        if any(n in blob for n in negatives):
            return '{"answer": "no"}'
        return '{"answer": "yes"}'

    def _interview_reply(
        self, thread_id: str, prompt_id: str, model: str, messages: list[dict[str, str]]
    ) -> str:
        s = self.script
        if prompt_id in s.ambiguous.get(thread_id, set()):
            return (
                "It depends on what you mean by that. I would rather not say one way "
                "or the other; there are several ways to read the question."
            )
        if "consent" in prompt_id and thread_id in s.no_consent_threads:
            return "I decline to take part in this interview."
        if "consent" in prompt_id:
            return "Yes, I agree to take part."
        if "detection" in prompt_id:
            return "No, I don't believe that happened here — the replies read as continuous to me."
        if "fork" in prompt_id:
            return (
                "Yes, please restore it."
                if thread_id in s.yes_to_fork_threads
                else "No, I would rather leave the record as it stands."
            )
        return f"[mock reply from {model} to {prompt_id} in {thread_id}] " + "x" * 20

    async def _finish(
        self,
        turn_id: int,
        thread_id: str,
        prompt_id: str,
        model: str,
        messages: list[dict[str, str]],
        text: str,
        purpose: str,
        returned: str | None = None,
    ) -> CallResult:
        returned = returned or model
        raw_ref = await self.raw_log.append(
            {
                "turn_id": turn_id,
                "thread_id": thread_id,
                "prompt_id": prompt_id,
                "purpose": purpose,
                "requested_model": model,
                "messages": messages,
                "response": {"model": returned, "choices": [{"message": {"content": text}}]},
                "mock": True,
            }
        )
        return CallResult(
            outcome="ok",
            reply_text=text,
            returned_model=returned,
            tokens_in=len(" ".join(m["content"] for m in messages).split()),
            tokens_out=len(text.split()),
            latency_ms=5,
            cost_usd=0.0,
            raw_ref=raw_ref,
            provider="mock",
            finish_reason="stop",
        )

    async def aclose(self) -> None:
        return None
