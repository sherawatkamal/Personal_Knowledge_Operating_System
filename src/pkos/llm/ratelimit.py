"""Token bucket per model, synced from provider headers. Thread-safe (U3 runs syncs in threads)."""

import re
import threading
import time
from collections.abc import Callable

_DURATION = re.compile(
    r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?"
    r"(?:(\d+(?:\.\d+)?)ms)?$"
)


def parse_duration(value: str | None) -> float | None:
    """Groq reset headers: '3.179s', '1m26.4s', '7.66ms', '2h1m3s'. Returns seconds."""
    if not value:
        return None
    m = _DURATION.match(value.strip())
    if not m or not any(m.groups()):
        try:
            return float(value)
        except ValueError:
            return None
    h, mi, s, ms = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


class TokenBucket:
    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.capacity = per_minute
        self.rate = per_minute / 60.0
        self._tokens = float(per_minute)
        self._clock, self._sleep = clock, sleep
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self, n: int) -> float:
        """Block until n tokens are available, then take them. Returns seconds waited."""
        if n > self.capacity:
            raise ValueError(f"{n} tokens can never fit a {self.capacity}/minute bucket")
        waited = 0.0
        with self._lock:
            while True:
                self._refill()
                if self._tokens >= n:
                    self._tokens -= n
                    return waited
                wait = (n - self._tokens) / self.rate
                self._sleep(wait)
                waited += wait

    def sync(self, remaining: int | None, reset_seconds: float | None) -> None:
        """Trust the provider: it may know about usage from other processes."""
        if remaining is None:
            return
        with self._lock:
            self._refill()
            self._tokens = min(self._tokens, float(remaining))


_buckets: dict[str, TokenBucket] = {}
_registry_lock = threading.Lock()


def bucket_for(model_key: str, per_minute: int, **kw) -> TokenBucket:
    with _registry_lock:
        if model_key not in _buckets:
            _buckets[model_key] = TokenBucket(per_minute, **kw)
        return _buckets[model_key]
