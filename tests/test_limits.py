import cramjam
import pytest

from datum.api.internals.remote import (
    BodyTooLarge,
    TooManyLabels,
    TooManySamples,
    TooManySeries,
    decode,
)
from datum.api.internals.remote.remote_write_pb2 import WriteRequest
from datum.api.internals.traces import decode as decode_traces
from datum.config import MAX_BATCH, MAX_DECOMPRESSED, MAX_LABELS

RESOURCE = "acme"


def test_a_small_body_that_decompresses_huge_is_refused(provider):
    """Snappy runs ~20:1, so the body cap does not bound this alone."""
    bomb = bytes(cramjam.snappy.compress_raw(b"\0" * (MAX_DECOMPRESSED + 1)))

    with pytest.raises(BodyTooLarge, match=str(MAX_DECOMPRESSED)):
        decode(bomb, RESOURCE)


def test_the_bomb_is_never_allocated_to_answer_it(client, provider):
    bomb = bytes(cramjam.snappy.compress_raw(b"\0" * (MAX_DECOMPRESSED + 1)))

    assert client.post("/v1/ingest/remote", content=bomb).status_code == 413
    assert provider.written == []


def test_a_gzipped_body_that_decompresses_huge_is_refused(provider):
    """The traces path shares the cap; gzip declares no length, so it is checked
    on what came out rather than on what was claimed."""
    bomb = bytes(cramjam.gzip.compress(b"\0" * (MAX_DECOMPRESSED + 1)))

    with pytest.raises(BodyTooLarge, match=str(MAX_DECOMPRESSED)):
        decode_traces(bomb, RESOURCE)


def series(count, samples=0):
    """A body of `count` series, each carrying a usable name."""
    request = WriteRequest()
    for _ in range(count):
        stream = request.timeseries.add()
        stream.labels.add(name="__name__", value="cpu")
        for index in range(samples):
            stream.samples.add(value=1.0, timestamp=1785836545000 + index)
    return bytes(cramjam.snappy.compress_raw(request.SerializeToString()))


def test_series_carrying_no_readings_are_refused_before_they_are_built():
    """MAX_BATCH counts readings, so empty series slip past it."""
    with pytest.raises(TooManySeries, match=str(MAX_BATCH)):
        decode(series(MAX_BATCH + 1), RESOURCE)


def test_the_series_cap_is_reached_without_parsing_the_body():
    """The scan stops early, so refusing costs the scan, not the parse."""
    body = series(MAX_BATCH * 20)
    with pytest.raises(TooManySeries):
        decode(body, RESOURCE)


def test_a_body_of_exactly_the_cap_is_still_accepted():
    assert len(decode(series(MAX_BATCH, samples=1), RESOURCE)) == MAX_BATCH


def test_an_empty_series_body_is_a_413(client, provider):
    assert client.post("/v1/ingest/remote", content=series(MAX_BATCH + 1)).status_code == 413
    assert provider.written == []


def test_the_scan_refuses_rubbish_as_a_400_not_a_500(client):
    """Decompresses, but is not protobuf: the scan sees it first."""
    body = bytes(cramjam.snappy.compress_raw(b"\xff" * 64))

    assert client.post("/v1/ingest/remote", content=body).status_code == 400


def one_series(labels=1, samples=0):
    """A single series, however wide or deep."""
    request = WriteRequest()
    stream = request.timeseries.add()
    stream.labels.add(name="__name__", value="cpu")
    for index in range(labels - 1):
        stream.labels.add(name=f"label_{index}", value="v")
    for index in range(samples):
        stream.samples.add(value=1.0, timestamp=1785836545000 + index)
    return bytes(cramjam.snappy.compress_raw(request.SerializeToString()))


def test_one_series_cannot_carry_a_whole_batch_past_the_cap():
    """Series count alone does not bound a body."""
    with pytest.raises(TooManySamples, match=str(MAX_BATCH)):
        decode(one_series(samples=MAX_BATCH + 1), RESOURCE)


def test_a_series_wider_than_anything_real_is_refused():
    with pytest.raises(TooManyLabels, match=str(MAX_LABELS)):
        decode(one_series(labels=MAX_LABELS + 1, samples=1), RESOURCE)


def test_a_series_of_exactly_the_label_cap_is_accepted():
    assert len(decode(one_series(labels=MAX_LABELS, samples=1), RESOURCE)) == 1


def test_a_wide_series_is_a_413(client, provider):
    body = one_series(labels=MAX_LABELS + 1, samples=1)

    assert client.post("/v1/ingest/remote", content=body).status_code == 413
    assert provider.written == []
