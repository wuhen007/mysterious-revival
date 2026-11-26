from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Callable

from ..engine.models import Identity


@dataclass
class _RequestWindow:
    timestamps: deque[float] = field(default_factory=deque)
    notice_sent: bool = False


class InMemoryRateLimiter:
    """Sliding-window command limit with one warning per saturated window."""

    def __init__(
        self,
        limit: int = 6,
        window_seconds: float = 5.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._windows: dict[tuple[str, str, str], _RequestWindow] = {}
        self._lock = Lock()
        self._next_sweep = 0.0

    @staticmethod
    def _key(identity: Identity) -> tuple[str, str, str]:
        return (identity.adapter_id, identity.user_id, identity.group_id)

    def _prune(self, window: _RequestWindow, now: float) -> None:
        cutoff = now - self.window_seconds
        while window.timestamps and window.timestamps[0] <= cutoff:
            window.timestamps.popleft()
        if not window.timestamps:
            window.notice_sent = False

    def check(self, identity: Identity) -> tuple[bool, bool]:
        """Return (allowed, notify_user); rejected repeats are silently suppressed."""
        now = self._clock()
        key = self._key(identity)
        with self._lock:
            if now >= self._next_sweep:
                for stale_key, stale_window in tuple(self._windows.items()):
                    self._prune(stale_window, now)
                    if not stale_window.timestamps:
                        self._windows.pop(stale_key, None)
                self._next_sweep = now + 60.0

            window = self._windows.get(key)
            if window is None:
                window = _RequestWindow()
                self._windows[key] = window
            else:
                self._prune(window, now)

            if len(window.timestamps) < self.limit:
                window.timestamps.append(now)
                return True, False

            notify = not window.notice_sent
            window.notice_sent = True
            return False, notify
