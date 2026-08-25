"""OTLP traces over HTTP: gzip off, protobuf out, rows in."""

from __future__ import annotations

import json
import zlib
from base64 import b64encode

from datum.api.internals.traces.traces_pb2 import ExportTraceServiceRequest
from datum.api.internals.wire import (
    LENGTH_DELIMITED,
    UNREADABLE,
    BodyTooLarge,
    read_varint,
    skip,
)
from datum.config.limits import (
    MAX_ATTRIBUTE,
    MAX_DECOMPRESSED,
    MAX_RESOURCE_ATTRIBUTES,
    MAX_SPAN_ATTRIBUTES,
    MAX_SPANS,
)

SERVICE_NAME = "service.name"

KINDS = ("UNSPECIFIED", "INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER")
STATUSES = ("UNSET", "OK", "ERROR")

RESOURCE_SPANS_FIELD = 1
RESOURCE_FIELD = 1
SCOPE_SPANS_FIELD = 2
SPAN_FIELD = 2
ATTRIBUTE_FIELD = 9
RESOURCE_ATTRIBUTE_FIELD = 1

GZIP_MAGIC = b"\x1f\x8b"
# Gzip rather than raw deflate.
GZIP_WBITS = 16 + zlib.MAX_WBITS


class TraceError(ValueError):
    """The body was not an OTLP ExportTraceServiceRequest."""


class TooManySpans(TraceError):
    """More spans than one request holds. Told apart so it answers 413."""


class TooManyAttributes(TraceError):
    """One span carrying more attributes than anything real does. Also a 413."""


class AttributeTooLarge(TraceError):
    """One attribute wider than a Map cell should hold. Also a 413."""


def decode(body: bytes, resource_id: str) -> list[dict]:
    """Table rows for one request, flattened out of their resources and scopes."""
    payload = _decompressed(body)
    _check_shape(payload)
    request = _parse(payload)

    rows = []
    for resource_spans in request.resource_spans:
        attributes = _attributes(resource_spans.resource.attributes)
        service = attributes.get(SERVICE_NAME, "")
        for scope in resource_spans.scope_spans:
            rows.extend(_row(span, resource_id, service, attributes) for span in scope.spans)
    return rows


def _row(span, resource_id: str, service: str, resource_attributes: dict) -> dict:
    attributes = {**resource_attributes, **_attributes(span.attributes)}
    # Each cap holds on its own, but the row carries both maps merged.
    if len(attributes) > MAX_SPAN_ATTRIBUTES:
        raise TooManyAttributes(
            f"a span would carry {len(attributes)} attributes once its resource's are "
            f"merged in, but {MAX_SPAN_ATTRIBUTES} is the cap"
        )
    return {
        "ts": span.start_time_unix_nano,
        "resource_id": resource_id,
        "service": service,
        "span_name": span.name,
        "span_kind": _named(KINDS, span.kind),
        "trace_id": span.trace_id.hex(),
        "span_id": span.span_id.hex(),
        "parent_span_id": span.parent_span_id.hex(),
        "duration_ns": max(span.end_time_unix_nano - span.start_time_unix_nano, 0),
        "status_code": _named(STATUSES, span.status.code),
        "status_message": span.status.message,
        "attributes": attributes,
    }


def _named(names: tuple[str, ...], value: int) -> str:
    return names[value] if 0 <= value < len(names) else names[0]


def _attributes(pairs) -> dict[str, str]:
    """OTel values are a union; the column is a string map."""
    return {pair.key: _value(pair.value) for pair in pairs}


def _value(value) -> str:
    """Arrays and kvlists become JSON rather than being dropped."""
    plain = _plain(value)
    if plain is None:
        return ""
    if isinstance(plain, str):
        return plain
    if isinstance(plain, bool):
        return "true" if plain else "false"
    if isinstance(plain, (int, float)):
        return str(plain)
    return json.dumps(plain, separators=(",", ":"))


def _plain(value):
    """One OTel value as a Python object, however deeply it nests."""
    fields = value.ListFields()
    if not fields:
        return None
    name = fields[0][0].name
    if name == "array_value":
        return [_plain(item) for item in value.array_value.values]
    if name == "kvlist_value":
        return {pair.key: _plain(pair.value) for pair in value.kvlist_value.values}
    if name == "bytes_value":
        return b64encode(value.bytes_value).decode()
    return getattr(value, name)


def _parse(payload: bytes) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(payload)
    except UNREADABLE as unreadable:
        raise TraceError(f"body is not an OTLP trace request: {unreadable}") from unreadable
    return request


def _decompressed(body: bytes) -> bytes:
    """Gzip declares no length, so the cap bounds what is allocated rather than
    being checked once it already is. One byte past the cap is enough to refuse.
    """
    if not body.startswith(GZIP_MAGIC):
        if len(body) > MAX_DECOMPRESSED:
            raise BodyTooLarge(f"body exceeds the cap of {MAX_DECOMPRESSED} bytes")
        return body
    stream = zlib.decompressobj(GZIP_WBITS)
    try:
        payload = stream.decompress(body, MAX_DECOMPRESSED + 1)
    except UNREADABLE as unreadable:
        raise TraceError(f"body is not gzip: {unreadable}") from unreadable

    if len(payload) > MAX_DECOMPRESSED:
        raise BodyTooLarge(f"body decompresses past the cap of {MAX_DECOMPRESSED} bytes")
    # Without this a truncated upload decodes to whatever arrived, and answering
    # 200 tells the collector to move on: the missing spans are never resent.
    if not stream.eof:
        raise TraceError("body is a truncated gzip stream")
    if stream.unused_data:
        raise TraceError("body carries data after its gzip stream")
    return payload


def _check_shape(payload: bytes) -> None:
    """Spans and their attributes, counted off the wire before protobuf builds them."""
    spans = _scan(payload)
    if spans > MAX_SPANS:
        raise TooManySpans(f"{spans} spans, but a request holds {MAX_SPANS}")


def _scan(payload: bytes) -> int:
    """Spans in one body, stopping at the first breach."""
    try:
        return _count(payload, 0, len(payload), RESOURCE_SPANS_FIELD, _scan_resource)
    except TraceError:
        raise
    except UNREADABLE as unreadable:
        raise TraceError(f"body is not an OTLP trace request: {unreadable}") from unreadable


def _scan_resource(payload: bytes, position: int, end: int) -> int:
    """A resource's attributes ride on every span it holds, so they meet the same
    caps as a span's own before anything is built."""
    _scan_resource_attributes(payload, position, end)
    return _count(payload, position, end, SCOPE_SPANS_FIELD, _scan_scope)


def _scan_resource_attributes(payload: bytes, position: int, end: int) -> None:
    """`resource` is a singular field, so protobuf merges repeats of it rather
    than replacing, and the attribute lists inside concatenate. The caps are on
    that merged total: checking each occurrence alone lets N of them through."""
    attributes = total = 0
    while position < end:
        tag, position = read_varint(payload, position)
        if (tag >> 3) != RESOURCE_FIELD or (tag & 7) != LENGTH_DELIMITED:
            position = skip(payload, position, tag & 7)
            continue
        length, position = read_varint(payload, position)
        found, bytes_used = _attributes_within(
            payload, position, position + length, RESOURCE_ATTRIBUTE_FIELD
        )
        attributes += found
        total += bytes_used
        position += length

    _refuse_over(attributes, total, "a resource", MAX_RESOURCE_ATTRIBUTES)


def _scan_scope(payload: bytes, position: int, end: int) -> int:
    return _count(payload, position, end, SPAN_FIELD, _scan_span)


def _scan_span(payload: bytes, position: int, end: int) -> int:
    """One span. Nothing bounds its attributes, so MAX_SPAN_ATTRIBUTES does."""
    attributes, total = _attributes_within(payload, position, end, ATTRIBUTE_FIELD)
    _refuse_over(attributes, total, "a span", None)
    return 1


def _attributes_within(payload: bytes, position: int, end: int, field: int) -> tuple[int, int]:
    """How many attributes one message carries and how many bytes they occupy.
    Only the per-attribute width is refused here; the totals are the caller's,
    which may be summing across more than one occurrence."""
    attributes = total = 0
    while position < end:
        tag, position = read_varint(payload, position)
        if (tag >> 3) != field or (tag & 7) != LENGTH_DELIMITED:
            position = skip(payload, position, tag & 7)
            continue
        length, position = read_varint(payload, position)
        if length > MAX_ATTRIBUTE:
            raise AttributeTooLarge(
                f"an attribute occupies {length} bytes, but {MAX_ATTRIBUTE} is the cap"
            )
        attributes += 1
        total += length
        position += length
    return attributes, total


def _refuse_over(attributes: int, total: int, subject: str, budget: int | None) -> None:
    if attributes > MAX_SPAN_ATTRIBUTES:
        raise TooManyAttributes(
            f"{subject} carries {attributes} attributes, but {MAX_SPAN_ATTRIBUTES} is the cap"
        )
    if budget is not None and total > budget:
        raise AttributeTooLarge(
            f"{subject} carries {total} bytes of attributes, but {budget} is the cap"
        )


def _count(payload: bytes, position: int, end: int, field: int, inner) -> int:
    """Spans under one nesting level, following `field` and skipping the rest."""
    spans = 0
    while position < end and spans <= MAX_SPANS:
        tag, position = read_varint(payload, position)
        if (tag >> 3) != field or (tag & 7) != LENGTH_DELIMITED:
            position = skip(payload, position, tag & 7)
            continue
        length, position = read_varint(payload, position)
        spans += inner(payload, position, position + length)
        position += length
    return spans
