"""OTLP traces over HTTP: gzip off, protobuf out, rows in."""

from __future__ import annotations

import json
from base64 import b64encode

import cramjam

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
    MAX_SPAN_ATTRIBUTES,
    MAX_SPANS,
)

SERVICE_NAME = "service.name"

KINDS = ("UNSPECIFIED", "INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER")
STATUSES = ("UNSET", "OK", "ERROR")

RESOURCE_SPANS_FIELD = 1
SCOPE_SPANS_FIELD = 2
SPAN_FIELD = 2
ATTRIBUTE_FIELD = 9

GZIP_MAGIC = b"\x1f\x8b"


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
        "attributes": {**resource_attributes, **_attributes(span.attributes)},
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
    """Gzip declares no length, so the cap is checked on what came out."""
    if not body.startswith(GZIP_MAGIC):
        return body
    try:
        payload = bytes(cramjam.gzip.decompress(body))
    except UNREADABLE as unreadable:
        raise TraceError(f"body is not gzip: {unreadable}") from unreadable
    if len(payload) > MAX_DECOMPRESSED:
        raise BodyTooLarge(f"{len(payload)} bytes decompressed, but the cap is {MAX_DECOMPRESSED}")
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
    return _count(payload, position, end, SCOPE_SPANS_FIELD, _scan_scope)


def _scan_scope(payload: bytes, position: int, end: int) -> int:
    return _count(payload, position, end, SPAN_FIELD, _scan_span)


def _scan_span(payload: bytes, position: int, end: int) -> int:
    """One span. Nothing bounds its attributes, so MAX_SPAN_ATTRIBUTES does."""
    attributes = 0
    while position < end:
        tag, position = read_varint(payload, position)
        if (tag >> 3) == ATTRIBUTE_FIELD and (tag & 7) == LENGTH_DELIMITED:
            attributes += 1
            position = _measured(payload, position)
            continue
        position = skip(payload, position, tag & 7)

    if attributes > MAX_SPAN_ATTRIBUTES:
        raise TooManyAttributes(
            f"a span carries {attributes} attributes, but {MAX_SPAN_ATTRIBUTES} is the cap"
        )
    return 1


def _measured(payload: bytes, position: int) -> int:
    """Past one attribute, refusing it if the wire says it is too wide."""
    length, position = read_varint(payload, position)
    if length > MAX_ATTRIBUTE:
        raise AttributeTooLarge(
            f"an attribute occupies {length} bytes, but {MAX_ATTRIBUTE} is the cap"
        )
    return position + length


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
