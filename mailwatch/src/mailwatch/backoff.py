"""Per-account exponential backoff for IMAP reconnect attempts.

The daemon's catch-up and training loops retry a broken IMAP connection
on a fixed schedule.  Against a provider that rate-limits authentication,
a tight retry loop turns a transient failure into a self-inflicted IP
block.  :class:`ConnectionBackoff` tracks consecutive failures per
account and tells the loops when the next attempt is due.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable


class ConnectionBackoff:
    """Per-account exponential backoff with jitter.

    The delay after the *n*-th consecutive failure is
    ``min(base_delay * factor ** (n - 1), max_delay)``, randomised by
    ``+/- jitter`` (a fraction) so concurrent accounts do not retry in
    lockstep.  A success resets the account's failure counter.

    Args:
        base_delay: Delay (seconds) before the second attempt.
        factor: Multiplier applied per consecutive failure.
        max_delay: Upper bound (seconds) on the delay.
        jitter: Fractional randomisation applied to each delay.
        clock: Monotonic clock, injectable for tests.
        rand: ``random.uniform``-compatible callable, injectable for tests.
    """

    def __init__(
        self,
        base_delay: float = 60.0,
        factor: float = 2.0,
        max_delay: float = 1800.0,
        jitter: float = 0.2,
        *,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.base_delay = base_delay
        self.factor = factor
        self.max_delay = max_delay
        self.jitter = jitter
        self._clock = clock
        self._rand = rand
        self._failures: dict[str, int] = {}
        self._next_attempt: dict[str, float] = {}
        self._lock = threading.Lock()

    def should_attempt(self, account_email: str) -> bool:
        """Return True when the account is outside its backoff window."""
        with self._lock:
            return self._clock() >= self._next_attempt.get(account_email, 0.0)

    def record_success(self, account_email: str) -> None:
        """Reset the account's failure counter and backoff window."""
        with self._lock:
            self._failures.pop(account_email, None)
            self._next_attempt.pop(account_email, None)

    def record_failure(self, account_email: str) -> float:
        """Record a failure and return the delay before the next attempt."""
        with self._lock:
            count = self._failures.get(account_email, 0) + 1
            self._failures[account_email] = count
            delay = min(
                self.base_delay * (self.factor ** (count - 1)), self.max_delay
            )
            if self.jitter:
                spread = delay * self.jitter
                delay = max(0.0, delay + self._rand(-spread, spread))
            self._next_attempt[account_email] = self._clock() + delay
            return delay

    def consecutive_failures(self, account_email: str) -> int:
        """Return the current consecutive-failure count for the account."""
        with self._lock:
            return self._failures.get(account_email, 0)


__all__ = ["ConnectionBackoff"]
