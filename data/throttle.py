"""Throttling and retry with back-off for outbound data calls.

Clock and sleep are injectable so tests never actually wait.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, TypeVar

import config

T = TypeVar("T")


class MinIntervalThrottle:
    """At most one call every `min_seconds` (the yfinance throttle)."""

    def __init__(self, min_seconds: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_seconds = min_seconds
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None:
                gap = now - self._last
                if gap < self.min_seconds:
                    self._sleep(self.min_seconds - gap)
                    now = self._clock()
            self._last = now


class RateLimiter:
    """At most `max_per_second` calls in any rolling one-second window (SEC fair access)."""

    def __init__(self, max_per_second: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.max_per_second = max_per_second
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            while self._calls and now - self._calls[0] >= 1.0:
                self._calls.popleft()
            if len(self._calls) >= self.max_per_second:
                self._sleep(1.0 - (now - self._calls[0]))
                now = self._clock()
                while self._calls and now - self._calls[0] >= 1.0:
                    self._calls.popleft()
            self._calls.append(now)


def with_retries(fn: Callable[[], T], *, retries: int = config.FETCH_MAX_RETRIES,
                 backoff_seconds: float = config.FETCH_BACKOFF_SECONDS,
                 sleep: Callable[[float], None] = time.sleep,
                 retry_on: tuple[type[BaseException], ...] = (Exception,)) -> T:
    """Call fn, retrying up to `retries` times with doubling back-off. Re-raises the last error."""
    delay = backoff_seconds
    for attempt in range(retries + 1):
        try:
            return fn()
        except retry_on:
            if attempt == retries:
                raise
            sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


YF_THROTTLE = MinIntervalThrottle(config.YF_MIN_SECONDS_BETWEEN_CALLS)
EDGAR_LIMITER = RateLimiter(config.EDGAR_MAX_REQUESTS_PER_SECOND)
