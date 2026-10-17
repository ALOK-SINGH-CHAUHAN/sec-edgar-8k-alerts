"""Tests for rate_limiter.py using fake clock and concurrent simulation."""

import asyncio
from collections import deque
import pytest
from rate_limiter import AsyncTokenBucket


class FakeClock:
    def __init__(self, start_time: float = 1000.0):
        self.current_time = start_time

    def now(self) -> float:
        return self.current_time

    async def sleep(self, duration: float) -> None:
        self.current_time += duration
        await asyncio.sleep(0)  # Yield control to let other tasks progress


def test_rate_limiter_rejects_exceeding_hard_cap():
    with pytest.raises(ValueError, match="SEC fair-access policy hard cap"):
        AsyncTokenBucket(rate=10.5)


@pytest.mark.asyncio
async def test_rate_limiter_never_exceeds_rate_in_any_one_second_window():
    clock = FakeClock()
    rate = 8.0
    limiter = AsyncTokenBucket(
        rate=rate,
        capacity=rate,
        time_func=clock.now,
        sleep_func=clock.sleep,
    )

    timestamps = []

    async def worker():
        for _ in range(5):
            await limiter.acquire(1.0)
            timestamps.append(clock.now())

    # Launch 6 concurrent workers (total 30 requests)
    workers = [asyncio.create_task(worker()) for _ in range(6)]
    await asyncio.gather(*workers)

    assert len(timestamps) == 30

    # Verify that in ANY 1.0 second window [t, t + 1.0], count <= rate (8)
    for i, t in enumerate(timestamps):
        window_end = t + 0.999999  # slightly strictly within 1 second
        count_in_window = sum(1 for ts in timestamps if t <= ts <= window_end)
        assert (
            count_in_window <= rate
        ), f"At t={t}, {count_in_window} requests occurred within 1s window (max allowed {rate})"


@pytest.mark.asyncio
async def test_rate_limiter_burst_behavior():
    clock = FakeClock()
    rate = 5.0
    limiter = AsyncTokenBucket(
        rate=rate,
        capacity=rate,
        time_func=clock.now,
        sleep_func=clock.sleep,
    )

    # Initial state has capacity (5 tokens).
    # Acquiring 5 tokens in immediate succession should all happen at t=1000.0
    for _ in range(5):
        await limiter.acquire(1.0)
        assert clock.now() == 1000.0

    # 6th acquire must wait because tokens exhausted and 1-second window is full
    await limiter.acquire(1.0)
    assert clock.now() > 1000.0
