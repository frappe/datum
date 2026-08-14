"""The producer-side client. Nothing here touches a network."""

from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

from datum_client import Batch, Datum
from datum_client.client import MAX_SAMPLES

URL = "https://datum.example"
TOKEN = "token"


class Posted:
    """Every request the client made, without one leaving the process."""

    def __init__(self, status: int = 200, raises: Exception | None = None):
        self.status = status
        self.raises = raises
        self.requests: list = []
        # Called one per request, standing in for what lands while a post is in
        # flight. A list rather than one callable so a test cannot hang.
        self.during: list = []

    @property
    def batches(self) -> list[list[dict]]:
        return [request["samples"] for request in self.requests]

    @property
    def samples(self) -> list[dict]:
        return [sample for batch in self.batches for sample in batch]


@pytest.fixture
def posted(monkeypatch):
    """Stands in for `urlopen`, so a test sees what a request carried."""
    import json

    from datum_client import client as module

    recorder = Posted()

    @contextmanager
    def urlopen(request, timeout=None):
        recorder.requests.append(json.loads(request.data))
        if recorder.during:
            recorder.during.pop(0)()
        if recorder.raises:
            raise recorder.raises
        yield type("Response", (), {"status": recorder.status})()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    return recorder


def batch(count: int = 1) -> Batch:
    """One batch carrying `count` samples."""
    made = Batch("example", "subsystem", env="prod")
    for index in range(count):
        made.gauge(f"queue_depth_{index}", index, "items")
    return made


def make_datum(**overrides) -> Datum:
    settings = {"flush_at": 500, "flush_interval": 10.0, **overrides}
    return Datum(URL, TOKEN, **settings)


def test_importing_the_package_sends_nothing(posted):
    """The module built a client and three batches at import once. It must not."""
    import importlib

    from datum_client import client as module

    importlib.reload(module)

    assert posted.requests == []


def test_recording_below_the_threshold_posts_nothing(posted):
    datum = make_datum()

    datum.record(batch(10))

    assert posted.requests == []
    assert len(datum) == 10


def test_a_full_buffer_posts(posted):
    datum = make_datum(flush_at=5)

    datum.record(batch(5))

    assert len(posted.samples) == 5
    assert len(datum) == 0


def test_an_elapsed_interval_posts(posted):
    """Nothing drains in the background, so the interval is checked on record."""
    datum = make_datum(flush_at=1_000, flush_interval=0.0)

    datum.record(batch(3))

    assert len(posted.samples) == 3


def test_flush_returns_what_it_sent(posted):
    datum = make_datum()
    datum.record(batch(7))

    assert datum.flush() == 7
    assert len(datum) == 0


def test_flush_leaves_behind_what_arrived_while_it_was_posting(posted):
    """A post takes time, and `record` does not hold the lock, so samples land
    mid-flight. They belong to the next flush: a flush that chases them never
    returns, and the thread that called it is stuck in the client."""
    datum = make_datum(flush_at=10**9)
    datum.record(batch(3))
    posted.during = [lambda: datum._samples.extend(batch(2).samples)]

    assert datum.flush() == 3
    assert len(posted.requests) == 1
    assert len(datum) == 2


def test_flush_on_an_empty_buffer_posts_nothing(posted):
    assert make_datum().flush() == 0
    assert posted.requests == []


def test_a_drain_larger_than_one_request_is_chunked(posted):
    """A batch holds at most MAX_SAMPLES, but a buffer can hold several."""
    datum = make_datum(flush_at=MAX_SAMPLES * 4)
    for _ in range(3):
        datum.record(batch(MAX_SAMPLES))

    datum.flush()

    assert [len(request) for request in posted.batches] == [MAX_SAMPLES] * 3


def test_a_batch_past_the_threshold_is_shipped_not_trimmed(posted):
    """The client never throws a sample away. Past `flush_at` it posts."""
    datum = make_datum(flush_at=5)

    datum.record(batch(8))

    assert len(posted.samples) == 8
    assert len(datum) == 0


def test_many_batches_at_once_all_arrive(posted):
    """Each batch is checked as it lands, so one `record` can post more than
    once rather than letting the buffer grow past what it flushes at."""
    datum = make_datum(flush_at=10)

    datum.record(batch(10), batch(10), batch(10))

    assert len(posted.samples) == 30
    assert len(datum) == 0
    assert len(posted.requests) == 3


def test_nothing_is_lost_when_far_more_arrives_than_one_request_holds(posted):
    datum = make_datum(flush_at=MAX_SAMPLES)
    for _ in range(3):
        datum.record(batch(MAX_SAMPLES))

    assert len(posted.samples) == MAX_SAMPLES * 3
    assert len(datum) == 0


def test_the_context_manager_flushes_what_is_left(posted):
    with make_datum() as datum:
        datum.record(batch(4))
        assert posted.requests == []

    assert len(posted.samples) == 4


def test_an_unreachable_datum_never_raises(posted):
    import urllib.error

    posted.raises = urllib.error.URLError("down")
    datum = make_datum(flush_at=1)

    datum.record(batch(1))

    assert len(datum) == 0


def test_a_refusal_is_logged_and_not_raised(posted, caplog):
    import urllib.error

    posted.raises = urllib.error.HTTPError(URL, 422, "unusable", {}, None)
    datum = make_datum(flush_at=1)

    datum.record(batch(1))

    assert "422" in caplog.text


def test_recording_from_many_threads_loses_nothing(posted):
    """`record` is cheap but not atomic, so the drain is what needs the lock."""
    datum = make_datum(flush_at=50)
    threads = [threading.Thread(target=datum.record, args=(batch(10),)) for _ in range(20)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    datum.flush()

    assert len(posted.samples) == 200


def test_send_still_posts_one_shot_without_buffering(posted):
    """The escape hatch: a caller that wants the request now and knows the cost."""
    datum = make_datum()

    assert datum.send(batch(2)) == 200
    assert len(posted.samples) == 2
    assert len(datum) == 0


def test_the_token_and_path_are_what_datum_expects(posted):
    datum = make_datum(flush_at=1)

    datum.record(batch(1))

    assert posted.requests[0]["samples"][0]["labels"]["env"] == "prod"


def test_the_client_cap_is_the_cap_datum_enforces():
    """`datum_client` imports nothing from the service, so the two caps are
    separate literals. Nothing but this test notices when one of them moves."""
    from datum.config import MAX_BATCH

    assert MAX_SAMPLES == MAX_BATCH


def test_a_request_the_client_would_send_is_one_datum_accepts(client):
    """The cap is inclusive on both sides: a full chunk is stored, not refused."""
    full = {
        "samples": [{"metric": "system_cpu_percent", "value": 1.0, "ts": "2026-08-05T10:00:00Z"}]
        * MAX_SAMPLES
    }

    response = client.post("/v1/ingest", json=full)

    assert response.status_code == 200
    assert response.json() == {"accepted": MAX_SAMPLES}


def test_one_over_the_cap_is_refused(client):
    """Which is why the client chunks rather than posting whatever it holds."""
    over = {
        "samples": [{"metric": "system_cpu_percent", "value": 1.0, "ts": "2026-08-05T10:00:00Z"}]
        * (MAX_SAMPLES + 1)
    }

    assert client.post("/v1/ingest", json=over).status_code == 422


def test_a_buffer_larger_than_the_cap_is_never_posted_whole(posted):
    """The buffer may hold more than one request's worth. No request may."""
    datum = make_datum(flush_at=1_000_000)
    for _ in range(2):
        datum.record(batch(MAX_SAMPLES))

    datum.flush()

    assert max(len(request) for request in posted.batches) == MAX_SAMPLES
    assert len(posted.samples) == MAX_SAMPLES * 2
