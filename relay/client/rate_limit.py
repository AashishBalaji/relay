"""A simple token-bucket rate limiter, enforced client-side regardless of
what the upstream API documents -- so a connector built against an API with
no documented limit (or one whose limit we haven't bothered to encode yet)
still can't hammer it, and connector authors get this for free instead of
writing their own throttling per connector."""
from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(
        self,
        max_calls: int,
        window_seconds: float,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        if max_calls <= 0:
            raise ValueError("max_calls must be positive")
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._call_times: deque[float] = deque()

    def acquire(self) -> None:
        now = self._clock()
        # Drop timestamps that have aged out of the window.
        while self._call_times and now - self._call_times[0] >= self.window_seconds:
            self._call_times.popleft()

        if len(self._call_times) >= self.max_calls:
            wait_for = self.window_seconds - (now - self._call_times[0])
            if wait_for > 0:
                self._sleep(wait_for)
            now = self._clock()
            while self._call_times and now - self._call_times[0] >= self.window_seconds:
                self._call_times.popleft()

        self._call_times.append(now)
