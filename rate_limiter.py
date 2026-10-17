"""Global async token-bucket rate limiter with sliding window enforcement."""

import asyncio
from collections import deque
import logging
import time
from typing import Callable, Deque, Optional

logger = logging.getLogger(__name__)


class AsyncTokenBucket:
    """
    Async token-bucket rate limiter shared across all SEC EDGAR requests.
    Enforces that:
    1. Average rate does not exceed max_requests_per_second (hard cap 10.0 req/s).
    2. Rolling 1-second window request count never exceeds max_requests_per_second.
    3. Thread/coroutine safe with asyncio.Lock.
    4. Supports customizable time and sleep functions for deterministic mock testing.
    """

    def __init__(
        self,
        rate: float = 8.0,
        capacity: Optional[float] = None,
        time_func: Optional[Callable[[], float]] = None,
        sleep_func: Optional[Callable[[float], any]] = None,
    ):
        if rate > 10.0:
            raise ValueError(
                f"Rate {rate} exceeds SEC fair-access policy hard cap of 10.0 req/s"
            )
        self.rate = float(rate)
        # Capacity can allow micro-bursts, but capped at rate for strict window compliance
        self.capacity = float(capacity if capacity is not None else rate)
        self.tokens = self.capacity
        self.time_func = time_func or time.monotonic
        self.sleep_func = sleep_func or asyncio.sleep
        self.last_update = self.time_func()
        self._timestamps: Deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Acquire permission to execute a request."""
        while True:
            wait_time = 0.0
            async with self._lock:
                now = self.time_func()

                # 1. Purge rolling 1-second timestamps
                while self._timestamps and (now - self._timestamps[0]) >= 1.0:
                    self._timestamps.popleft()

                # 2. Refill tokens
                elapsed = max(0.0, now - self.last_update)
                self.last_update = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

                # Check strict rolling window limit
                if len(self._timestamps) >= int(self.rate):
                    # Must wait until oldest timestamp rolls off 1 second
                    wait_time = max(wait_time, (self._timestamps[0] + 1.0) - now)

                # Check token bucket availability
                if self.tokens < tokens:
                    token_wait = (tokens - self.tokens) / self.rate
                    wait_time = max(wait_time, token_wait)

                if wait_time <= 0.0:
                    # Successfully acquired
                    self.tokens -= tokens
                    self._timestamps.append(now)
                    return

            # Wait outside lock so other coroutines can check state
            await self.sleep_func(max(0.001, wait_time))
