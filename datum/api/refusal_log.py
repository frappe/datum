from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic


@dataclass
class RefusalLog:
    """How often to log the refusals of one sender, route and status, counted in this process.

    A sender over its rate limit can retry many times a second, and one line for each
    refusal would make Datum the largest writer of its own logs. So a line is written at
    most once a period, and it carries the refusals since the line before it.
    """

    period: float = 60
    clock: Callable[[], float] = monotonic
    lines: dict[tuple[str, str, int], tuple[float, int]] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)

    def count_refusal(self, sender: str, route: str, status: int) -> int:
        """Count one refusal. Return the refusals to log now, or 0 while the last line is recent."""
        key = (sender, route, status)
        now = self.clock()
        with self.lock:
            written_at, held = self.lines.get(key, (None, 0))
            if written_at is not None and now - written_at < self.period:
                self.lines[key] = (written_at, held + 1)
                return 0

            self.lines[key] = (now, 0)
            return held + 1
