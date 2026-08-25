"""Prometheus remote write v1: snappy off, protobuf out, rows in."""

from __future__ import annotations

from datetime import UTC, datetime

import cramjam

from datum.api.internals.remote.remote_write_pb2 import WriteRequest
from datum.api.internals.schemas import NAME
from datum.api.internals.wire import (
    LENGTH_DELIMITED,
    UNREADABLE,
    BodyTooLarge,
    read_varint,
    skip,
)
from datum.config.limits import MAX_BATCH, MAX_DECOMPRESSED, MAX_LABELS

NAME_LABEL = "__name__"
# A v2 request interns its strings, so decoding it as v1 yields labels that are not there.
V2 = "io.prometheus.write.v2.request"

SERIES_FIELD = 1
LABEL_FIELD = 1
SAMPLE_FIELD = 2


class RemoteWriteError(ValueError):
    """The body was not a snappy-compressed v1 WriteRequest."""


class TooManySamples(RemoteWriteError):
    """More samples than one batch holds. Told apart so it answers 413."""


class TooManySeries(RemoteWriteError):
    """More series than readings could fill. Also a 413."""


class TooManyLabels(RemoteWriteError):
    """One series carrying more labels than anything real does. Also a 413."""


def is_version_two(content_type: str) -> bool:
    return V2 in content_type.lower()


def decode(body: bytes, resource_id: str) -> list[dict]:
    """Table rows for one request, flattened out of their series.

    Built directly rather than through `Sample`: a name and its labels belong to
    the series, so they are checked once however many readings hang off them.
    """
    request = _parse(body)
    total = sum(len(series.samples) for series in request.timeseries)
    if total > MAX_BATCH:
        raise TooManySamples(f"{total} samples, but a batch holds {MAX_BATCH}")

    rows = []
    for series in request.timeseries:
        metric, labels = _series(series)
        rows.extend(
            {
                "ts": datetime.fromtimestamp(sample.timestamp / 1000, UTC),
                "metric": metric,
                "resource_id": resource_id,
                "labels": labels,
                "value": sample.value,
            }
            for sample in series.samples
        )
    return rows


def _series(series) -> tuple[str, dict[str, str]]:
    """The metric and labels every reading in one series shares.

    `resource_id` is dropped: the token decides it, not the producer.
    """
    metric = ""
    labels = {}
    for label in series.labels:
        if label.name == NAME_LABEL:
            metric = label.value
        elif label.name != "resource_id":
            labels[label.name] = label.value

    if not metric:
        raise RemoteWriteError("a series carries no __name__ label")
    for name in (metric, *labels):
        if not NAME.match(name):
            raise RemoteWriteError(f"{name!r} cannot be stored: it must match {NAME.pattern}")
    return metric, labels


def _parse(body: bytes) -> WriteRequest:
    """Decompressed outside the `try`, so an oversized body stays a 413."""
    payload = _decompressed(body)
    _check_shape(payload)
    request = WriteRequest()
    try:
        request.ParseFromString(payload)
    except UNREADABLE as unreadable:
        raise RemoteWriteError(f"body is not a v1 WriteRequest: {unreadable}") from unreadable
    return request


def _decompressed(body: bytes) -> bytes:
    """Sized from the snappy header, so a body small on the wire cannot become a
    large one in memory."""
    declared = _declared_length(body)
    if declared > MAX_DECOMPRESSED:
        raise BodyTooLarge(f"{declared} bytes decompressed, but the cap is {MAX_DECOMPRESSED}")

    buffer = bytearray(declared)
    try:
        written = cramjam.snappy.decompress_raw_into(body, buffer)
    except UNREADABLE as unreadable:
        raise RemoteWriteError(f"body is not snappy: {unreadable}") from unreadable
    return bytes(memoryview(buffer)[:written])


def _check_shape(payload: bytes) -> None:
    """What the body holds, counted off the wire before protobuf builds it.

    The objects are the cost, not the bytes: a series is ~193 bytes parsed
    against 17 sent.
    """
    series, samples = _scan(payload)
    if series > MAX_BATCH:
        raise TooManySeries(f"more than {MAX_BATCH} series, and each holds at least one reading")
    if samples > MAX_BATCH:
        raise TooManySamples(f"{samples} samples, but a batch holds {MAX_BATCH}")


def _scan(payload: bytes) -> tuple[int, int]:
    """Series and readings in one body, stopping at the first breach."""
    position = 0
    series = samples = 0
    try:
        while position < len(payload) and series <= MAX_BATCH and samples <= MAX_BATCH:
            tag, position = read_varint(payload, position)
            if (tag >> 3) != SERIES_FIELD or (tag & 7) != LENGTH_DELIMITED:
                position = skip(payload, position, tag & 7)
                continue
            length, position = read_varint(payload, position)
            series += 1
            samples += _scan_series(payload, position, position + length)
            position += length
    except RemoteWriteError:
        raise
    except UNREADABLE as unreadable:
        raise RemoteWriteError(f"body is not a v1 WriteRequest: {unreadable}") from unreadable
    return series, samples


def _scan_series(payload: bytes, position: int, end: int) -> int:
    """Readings in one series. Nothing bounds its labels, so MAX_LABELS does."""
    labels = samples = 0
    while position < end:
        tag, position = read_varint(payload, position)
        labels += (tag >> 3) == LABEL_FIELD
        samples += (tag >> 3) == SAMPLE_FIELD
        position = skip(payload, position, tag & 7)

    if labels > MAX_LABELS:
        raise TooManyLabels(f"a series carries {labels} labels, but {MAX_LABELS} is the cap")
    return samples


def _declared_length(body: bytes) -> int:
    """What the snappy header claims. Checked before it is allocated against."""
    try:
        return cramjam.snappy.decompress_raw_len(body)
    except UNREADABLE as unreadable:
        raise RemoteWriteError(f"body is not snappy: {unreadable}") from unreadable
