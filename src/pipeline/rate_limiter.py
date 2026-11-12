"""Timestamp-based token bucket rate limiter.

The bucket refills continuously from wall-clock time (``time.monotonic``)
rather than from a fixed per-request sleep. With the strict defaults
(capacity=1, refill_rate=10) at most one token is ever available, so request
starts are paced exactly 1/refill_rate seconds apart and **no one-second
window can contain more than refill_rate starts** — the hard ceiling the
pipeline requires.

Raising capacity above 1 permits bursts of up to ``capacity`` consecutive
starts (trading away the strict one-second ceiling); the strict default is
kept deliberately. Both values are configurable at runtime.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class TokenBucket:
    """Blocking token bucket with timestamp-based refill.

    Each refill is derived from wall-clock time, and every wait is floored at
    ``MIN_WAIT``: if a computed wait is smaller than the resolution the clock
    can represent, non-progress would let the loop spin forever. With the
    strict 10/sec default the steady-state wait is 0.1s, so the 100µs floor
    is invisible to the pacing guarantee.
    """

    MIN_WAIT = 1e-4

    def __init__(
        self,
        capacity: float = 1,
        refill_rate: float = 10,
        *,
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be > 0")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._tokens = float(capacity)
        self._last = self._clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last = now

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available, then consume them."""
        if tokens <= 0:
            return
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.refill_rate
                wait = max(wait, self.MIN_WAIT)
            self._sleeper(wait)

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens