"""Per-model request pacing.

OpenRouter enforces rate limits per model, not per key — and new accounts are
capped hard (10 requests/minute on some models). A worker pool of ten threads
that happen to share a resident will blow through that in seconds, which is
exactly how the first fleet attempt halted.

Pacing beats retrying: a sliding window per model keeps us under the limit
instead of discovering it, and the cost is only wall-clock on the models that
are actually busy.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class ModelRateLimiter:
    """Sliding-window limiter, one window per model string."""

    def __init__(self, default_rpm: int | None, overrides: dict[str, int] | None = None):
        self.default_rpm = default_rpm
        self.overrides = overrides or {}
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def limit_for(self, model: str) -> int | None:
        return self.overrides.get(model, self.default_rpm)

    async def acquire(self, model: str) -> float:
        """Block until a request to ``model`` is within budget. Returns seconds waited."""
        rpm = self.limit_for(model)
        if not rpm or rpm <= 0:
            return 0.0
        waited = 0.0
        async with self._locks[model]:
            while True:
                now = time.monotonic()
                window = self._calls[model]
                while window and now - window[0] >= 60.0:
                    window.popleft()
                if len(window) < rpm:
                    window.append(now)
                    return waited
                # Sleep until the oldest call leaves the window.
                delay = 60.0 - (now - window[0]) + 0.05
                waited += delay
                await asyncio.sleep(delay)
