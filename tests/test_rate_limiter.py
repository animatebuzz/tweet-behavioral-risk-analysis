"""Token-bucket property tests using a fake clock.

The central guarantee of the strict default (capacity=1, refill=10):
no one-second window contains more than 10 request starts.
"""

from __future__ import annotations

import pytest

from src.pipeline.rate_limiter import TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_bucket(capacity: float, refill: float):
    clock = FakeClock()
    bucket = TokenBucket(capacity, refill, clock=clock, sleeper=clock.advance)
    return bucket, clock


def acquire_many(bucket, clock, count: int) -> list[float]:
    """Record the instant each request is actually granted the token."""
    starts: list[float] = []
    for _ in range(count):
        bucket.acquire()
        starts.append(clock.now)
    return starts


def starts_per_window(starts: list[float], window: float = 1.0) -> int:
    worst = 0
    for start in starts:
        count = sum(1 for s in starts if start <= s < start + window)
        worst = max(worst, count)
    return worst


def test_strict_default_no_more_than_10_starts_per_window() -> None:
    bucket, clock = make_bucket(capacity=1, refill=10)
    start_times = acquire_many(bucket, clock, 35)
    assert starts_per_window(start_times) <= 10


def test_strict_default_initial_start_is_immediate() -> None:
    bucket, clock = make_bucket(capacity=1, refill=10)
    start_times = acquire_many(bucket, clock, 1)
    assert start_times[0] == 0.0


def test_strict_default_spacing_approaches_100ms() -> None:
    bucket, clock = make_bucket(capacity=1, refill=10)
    start_times = acquire_many(bucket, clock, 20)
    gaps = [b - a for a, b in zip(start_times, start_times[1:])]
    assert max(gaps) <= 0.101
    assert min(gaps) >= 0.095


def test_raised_capacity_allows_burst_but_is_opt_in() -> None:
    bucket, clock = make_bucket(capacity=10, refill=10)
    start_times = acquire_many(bucket, clock, 10)
    assert len(set(start_times)) == 1  # burst: all ten at t=0


def test_capacity_one_never_bursts_even_after_idle() -> None:
    bucket, clock = make_bucket(capacity=1, refill=10)
    start_times = acquire_many(bucket, clock, 3)
    clock.advance(5.0)  # idle long enough to refill fully
    start_times = acquire_many(bucket, clock, 3)
    assert len(set(start_times)) == 3


def test_invalid_configuration_rejected() -> None:
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_rate=10)
    with pytest.raises(ValueError):
        TokenBucket(capacity=1, refill_rate=0)


def test_subresolution_waits_never_starve_the_loop() -> None:
    """Regression: a deficit of one ULP must not spin forever (float
    starvation when the computed wait is below the clock's resolution)."""
    for capacity, refill in ((1, 10), (3, 10), (10, 10), (1, 3.7), (5, 0.25)):
        bucket, clock = make_bucket(capacity=capacity, refill=refill)
        acquire_many(bucket, clock, 300)


def test_strict_ceiling_holds_for_many_speeds() -> None:
    for refill in (1, 3.7, 10, 25):
        bucket, clock = make_bucket(capacity=1, refill=refill)
        start_times = acquire_many(bucket, clock, 500)
        assert starts_per_window(start_times, window=1.0) <= int(refill) + 1