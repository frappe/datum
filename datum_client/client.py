from __future__ import annotations

import atexit
import json
import logging
import urllib.error
import urllib.request
import weakref
from collections import deque
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from typing import Self, TypedDict

from datum_client.naming import build, validate_labels

logger = logging.getLogger("datum")

TIMEOUT = 2.0

# Samples in one request. This is datum's `MAX_BATCH`, and a request over it is
# a 422. Nothing here may import from the service, so the number is repeated
# rather than shared, and `tests/test_client.py` is what catches it drifting.
MAX_SAMPLES = 10_000

FLUSH_AT = 500
FLUSH_INTERVAL = 10.0


class MetricSample(TypedDict):
    metric: str
    value: float
    ts: str
    labels: dict[str, str]


class Batch:
    """Samples for one namespace, named the same way every time.

    `namespace` and `subsystem` are fixed here so call sites pass only what
    varies, which is what keeps names consistent across producers.
    """

    def __init__(self, namespace: str, subsystem: str = "", **labels):
        self.namespace = namespace
        self.subsystem = subsystem
        self.labels = validate_labels(labels)
        self.samples: list[dict] = []

    def gauge(self, target: str, value: float, unit: str = "", ts=None, **labels) -> Batch:
        """A value that goes up and down: a percentage, a size, a queue depth."""
        return self._add(build(self.namespace, self.subsystem, target, unit), value, ts, labels)

    def counter(self, target: str, value: float, unit: str = "", ts=None, **labels) -> Batch:
        """A value that only climbs. Gets `_total`, so a reader knows to diff it."""
        name = build(self.namespace, self.subsystem, target, unit, "total")
        return self._add(name, value, ts, labels)

    def up(self, target: str, is_up: bool, ts=None, **labels) -> Batch:
        """The `up` convention: absence is invisible, 0 is alertable."""
        return self._add(
            build(self.namespace, self.subsystem, target, "", "up"), int(is_up), ts, labels
        )

    def info(self, target: str, ts=None, **labels) -> Batch:
        """Always 1, carrying facts as labels. The one place a pid belongs."""
        name = build(self.namespace, self.subsystem, target, "", "info")
        return self._add(name, 1, ts, labels, churning_allowed=True)

    def _add(self, name: str, value: float, ts, labels: dict, churning_allowed=False) -> Batch:
        if len(self.samples) >= MAX_SAMPLES:
            raise ValueError(f"a batch holds at most {MAX_SAMPLES} samples")
        moment = ts or datetime.now(UTC)
        self.samples.append(
            MetricSample(
                metric=name,
                value=float(value),
                ts=moment.isoformat().replace("+00:00", "Z"),
                labels={**self.labels, **validate_labels(labels, churning_allowed)},
            )
        )
        return self

    def __len__(self) -> int:
        return len(self.samples)


class Datum:
    """Fire and forget: one POST, a short timeout, no spool and no retry.

    A dropped metric is a gap in a chart. Blocking a producer's collection tick
    to retry one is worse, so nothing here raises on a network failure.

    `record` buffers and drains when the buffer reaches `flush_at` or the
    interval has passed, so a caller never posts per sample. There is no
    background thread: the drain runs on whichever `record` finds one due, which
    is also why `flush` is public — a host with a scheduler should call it and
    take that cost off the producer entirely.

    The buffer has no ceiling because it is never trimmed: a sample leaves it by
    being posted, never by being dropped. What bounds it is that reaching
    `flush_at` ships, and a failed post drops on the wire rather than returning
    to the buffer, so it cannot grow while datum is down.

    `token` is a JWT that names a `resource_id`; datum stamps every row with it,
    so nothing here says where the samples came from.
    """

    def __init__(
        self,
        url: str,
        token: str,
        timeout: float = TIMEOUT,
        flush_at: int = FLUSH_AT,
        flush_interval: float = FLUSH_INTERVAL,
    ):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.flush_at = flush_at
        self.flush_interval = flush_interval
        self._samples: deque[dict] = deque()
        self._lock = Lock()
        self._flushed_at = monotonic()
        _LIVE.add(self)

    def record(self, *batches: Batch) -> None:
        """Buffer samples, draining after any batch that leaves one owed.

        Checked per batch rather than once at the end, so the buffer never holds
        more than `flush_at` plus one batch however many are handed over.
        """
        for batch in batches:
            self._samples.extend(batch.samples)
            if self.is_due:
                self.flush()

    def flush(self) -> int:
        """Post what is buffered now, in requests of at most `MAX_SAMPLES`.

        Bounded by what is owed on entry, not by what the buffer holds as it
        goes: `record` does not take the lock, so samples land mid-post. Chasing
        them means the producer thread that called this never gets out of it.
        """
        sent = 0
        with self._lock:
            owed = len(self._samples)
            while owed:
                taken = self._taken(owed)
                self._post(taken)
                owed -= len(taken)
                sent += len(taken)
            self._flushed_at = monotonic()
        return sent

    def send(self, *batches: Batch) -> int:
        """Post one or more batches as a single request, skipping the buffer.

        For a caller that wants the request now and accepts paying for it.
        """
        samples = [sample for batch in batches for sample in batch.samples]
        if not samples:
            return 0
        if len(samples) > MAX_SAMPLES:
            raise ValueError(f"{len(samples)} samples, but a request holds {MAX_SAMPLES}")
        return self._post(samples)

    @property
    def is_due(self) -> bool:
        return (
            len(self._samples) >= self.flush_at
            or monotonic() - self._flushed_at >= self.flush_interval
        )

    def close(self) -> int:
        sent = self.flush()
        _LIVE.discard(self)
        return sent

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception) -> None:
        self.close()

    def __len__(self) -> int:
        return len(self._samples)

    def _taken(self, owed: int) -> list[dict]:
        """One request's worth, off the front of the buffer."""
        return [self._samples.popleft() for _ in range(min(MAX_SAMPLES, owed))]

    def _post(self, samples: list[dict]) -> int:
        """One request. Returns datum's status, or 0 if it could not be reached."""
        request = urllib.request.Request(
            f"{self.url}/v1/ingest",
            data=json.dumps({"samples": samples}).encode(),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self.token}")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status
        except urllib.error.HTTPError as refused:
            # A 401 or 422 is our bug, not a blip. Silence here means metrics
            # stop and nobody notices, so it is logged even though we go on.
            logger.warning("datum refused %d: %s", refused.code, refused.read()[:500])
            return refused.code
        except (urllib.error.URLError, TimeoutError) as unreachable:
            logger.debug("datum unreachable: %s", unreachable)
            return 0


# Weak, so holding a client for the exit flush does not keep it alive.
_LIVE: weakref.WeakSet[Datum] = weakref.WeakSet()


@atexit.register
def _flush_live() -> None:
    """The tail a quiet process would otherwise hold until it died."""
    for client in list(_LIVE):
        client.flush()
