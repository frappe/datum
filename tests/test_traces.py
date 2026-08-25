import cramjam
import pytest

from datum.api.internals.traces import (
    AttributeTooLarge,
    TooManyAttributes,
    TooManySpans,
    TraceError,
    decode,
)
from datum.api.internals.traces.traces_pb2 import (
    ExportTraceServiceRequest,
    Resource,
    ScopeSpans,
)
from datum.config import (
    MAX_ATTRIBUTE,
    MAX_RESOURCE_ATTRIBUTES,
    MAX_SPAN_ATTRIBUTES,
    MAX_SPANS,
)

PATH = "/v1/traces"
RESOURCE = "acme"
TRACE = bytes.fromhex("4bf92f3577b34da6a3ce929d0e0e4736")
SPAN = bytes.fromhex("00f067aa0ba902b7")
START = 1787000000000000000


def exported(*resources, gzip=False) -> bytes:
    """One OTLP body, the way a collector would send it."""
    request = ExportTraceServiceRequest()
    for attributes, spans in resources:
        resource_spans = request.resource_spans.add()
        for key, value in attributes.items():
            pair = resource_spans.resource.attributes.add(key=key)
            pair.value.string_value = value
        scope = resource_spans.scope_spans.add()
        for span in spans:
            scope.spans.add(**span)
    body = request.SerializeToString()
    return bytes(cramjam.gzip.compress(body)) if gzip else body


def _gzipped(*resources) -> bytes:
    return bytes(cramjam.gzip.compress(exported(*resources)))


def span(**overrides) -> dict:
    return {
        "trace_id": TRACE,
        "span_id": SPAN,
        "name": "llm_request",
        "kind": 2,
        "start_time_unix_nano": START,
        "end_time_unix_nano": START + 1_240_000_000,
        **overrides,
    }


def test_a_span_becomes_one_row():
    rows = decode(exported(({"service.name": "vllm"}, [span()])), RESOURCE)

    assert len(rows) == 1
    row = rows[0]
    assert row["resource_id"] == RESOURCE
    assert row["service"] == "vllm"
    assert row["span_name"] == "llm_request"
    assert row["span_kind"] == "SERVER"
    assert row["trace_id"] == TRACE.hex()
    assert row["span_id"] == SPAN.hex()
    assert row["parent_span_id"] == ""
    assert row["duration_ns"] == 1_240_000_000


def test_the_timestamp_keeps_nanoseconds():
    rows = decode(exported(({}, [span(start_time_unix_nano=START + 123)])), RESOURCE)

    assert rows[0]["ts"] == START + 123


def test_a_gzipped_body_is_read():
    rows = decode(exported(({"service.name": "vllm"}, [span()]), gzip=True), RESOURCE)

    assert rows[0]["service"] == "vllm"


def test_resource_and_span_attributes_are_merged():
    one = span()
    rows = decode(exported(({"host.name": "gpu-04"}, [one])), RESOURCE)
    assert rows[0]["attributes"]["host.name"] == "gpu-04"


def test_span_attributes_carry_every_value_kind():
    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    one = scope.spans.add(**span())
    one.attributes.add(key="model").value.string_value = "llama"
    one.attributes.add(key="tokens").value.int_value = 1843
    one.attributes.add(key="temperature").value.double_value = 0.7
    one.attributes.add(key="stream").value.bool_value = True

    attributes = decode(request.SerializeToString(), RESOURCE)[0]["attributes"]

    assert attributes == {
        "model": "llama",
        "tokens": "1843",
        "temperature": "0.7",
        "stream": "true",
    }


def test_complex_attribute_values_are_kept_as_json():
    """An exporter sending an array or a kvlist is sending a valid attribute.
    Storing an empty string for it loses data and still answers success."""
    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    one = scope.spans.add(**span())
    stop = one.attributes.add(key="stop_sequences").value.array_value
    stop.values.add().string_value = "</s>"
    stop.values.add().int_value = 13
    sampling = one.attributes.add(key="sampling").value.kvlist_value
    sampling.values.add(key="top_p").value.double_value = 0.95
    one.attributes.add(key="digest").value.bytes_value = b"\x00\x01\x02"

    attributes = decode(request.SerializeToString(), RESOURCE)[0]["attributes"]

    assert attributes["stop_sequences"] == '["</s>",13]'
    assert attributes["sampling"] == '{"top_p":0.95}'
    assert attributes["digest"] == "AAEC"


def test_a_nested_array_keeps_its_shape():
    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    one = scope.spans.add(**span())
    outer = one.attributes.add(key="matrix").value.array_value
    inner = outer.values.add().array_value
    inner.values.add().int_value = 1
    inner.values.add().int_value = 2

    attributes = decode(request.SerializeToString(), RESOURCE)[0]["attributes"]

    assert attributes["matrix"] == "[[1,2]]"


def test_an_empty_array_is_not_a_missing_value():
    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    one = scope.spans.add(**span())
    one.attributes.add(key="empty").value.array_value.SetInParent()

    assert decode(request.SerializeToString(), RESOURCE)[0]["attributes"]["empty"] == "[]"


def test_the_status_is_named_not_numbered():
    one = span()
    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    built = scope.spans.add(**one)
    built.status.code = 2
    built.status.message = "out of memory"

    row = decode(request.SerializeToString(), RESOURCE)[0]

    assert row["status_code"] == "ERROR"
    assert row["status_message"] == "out of memory"


def test_a_span_without_a_status_is_unset():
    assert decode(exported(({}, [span()])), RESOURCE)[0]["status_code"] == "UNSET"


def test_spans_are_flattened_across_resources_and_scopes():
    body = exported(({"service.name": "a"}, [span(), span()]), ({"service.name": "b"}, [span()]))

    rows = decode(body, RESOURCE)

    assert [row["service"] for row in rows] == ["a", "a", "b"]


def test_the_token_owns_the_resource_id():
    """A producer naming itself in the attributes does not get to."""
    rows = decode(exported(({"resource_id": "elsewhere"}, [span()])), RESOURCE)

    assert rows[0]["resource_id"] == RESOURCE


def test_too_many_spans_is_refused():
    body = exported(({}, [span() for _ in range(MAX_SPANS + 1)]))

    with pytest.raises(TooManySpans, match=str(MAX_SPANS)):
        decode(body, RESOURCE)


def test_a_span_carrying_too_many_attributes_is_refused():
    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    one = scope.spans.add(**span())
    for index in range(MAX_SPAN_ATTRIBUTES + 1):
        one.attributes.add(key=f"key_{index}").value.string_value = "v"

    with pytest.raises(TraceError, match=str(MAX_SPAN_ATTRIBUTES)):
        decode(request.SerializeToString(), RESOURCE)


def test_a_single_huge_attribute_is_refused():
    """Counting attributes does not bound their width: one array can hold
    megabytes and still be one attribute."""
    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    one = scope.spans.add(**span())
    wide = one.attributes.add(key="wide").value.array_value
    for index in range(MAX_ATTRIBUTE):
        wide.values.add().string_value = f"token_{index}"

    with pytest.raises(AttributeTooLarge, match=str(MAX_ATTRIBUTE)):
        decode(request.SerializeToString(), RESOURCE)


def test_an_attribute_just_under_the_cap_is_kept():
    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    one = scope.spans.add(**span())
    one.attributes.add(key="prompt").value.string_value = "x" * (MAX_ATTRIBUTE - 100)

    attributes = decode(request.SerializeToString(), RESOURCE)[0]["attributes"]

    assert len(attributes["prompt"]) == MAX_ATTRIBUTE - 100


def test_nesting_deeper_than_protobuf_allows_is_a_400_not_a_500():
    """protobuf refuses ~100 nested messages itself, so `_plain` never recurses
    far enough to raise RecursionError."""
    request = ExportTraceServiceRequest()
    scope = request.resource_spans.add().scope_spans.add()
    value = scope.spans.add(**span()).attributes.add(key="deep").value
    for _ in range(60):
        value = value.array_value.values.add()
    value.string_value = "bottom"

    with pytest.raises(TraceError):
        decode(request.SerializeToString(), RESOURCE)


def test_a_resource_carrying_too_many_attributes_is_refused():
    """Resource attributes are copied onto every span the resource holds, so one
    request could store them thousands of times over."""
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    for index in range(MAX_SPAN_ATTRIBUTES + 1):
        resource_spans.resource.attributes.add(key=f"pad_{index}").value.string_value = "v"
    resource_spans.scope_spans.add().spans.add(**span())

    with pytest.raises(TooManyAttributes, match="a resource"):
        decode(request.SerializeToString(), RESOURCE)


def test_a_huge_resource_attribute_is_refused():
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.attributes.add(key="huge").value.string_value = "v" * MAX_ATTRIBUTE
    resource_spans.scope_spans.add().spans.add(**span())

    with pytest.raises(AttributeTooLarge):
        decode(request.SerializeToString(), RESOURCE)


def test_a_row_is_capped_on_both_maps_merged():
    """Each map can pass its own cap and still exceed it once merged, and the
    merged map is what a row stores."""
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    for index in range(MAX_SPAN_ATTRIBUTES - 10):
        resource_spans.resource.attributes.add(key=f"r_{index}").value.string_value = "v"
    one = resource_spans.scope_spans.add().spans.add(**span())
    for index in range(MAX_SPAN_ATTRIBUTES - 10):
        one.attributes.add(key=f"s_{index}").value.string_value = "v"

    with pytest.raises(TooManyAttributes, match="merged"):
        decode(request.SerializeToString(), RESOURCE)


def test_a_truncated_gzip_stream_is_refused():
    """A partial upload that decodes to fewer spans must not answer success: a
    collector treats 2xx as delivered and never sends them again."""
    whole = _gzipped(({"service.name": "vllm"}, [span(), span(), span()]))

    for cut in range(20, len(whole)):
        with pytest.raises(TraceError):
            decode(whole[:cut], RESOURCE)


def test_data_after_the_gzip_stream_is_refused():
    whole = _gzipped(({"service.name": "vllm"}, [span()]))

    with pytest.raises(TraceError, match="after its gzip stream"):
        decode(whole + b"junk", RESOURCE)


def test_a_resource_carrying_too_many_attribute_bytes_is_refused():
    """Each attribute can pass its own width cap while the resource as a whole
    is copied onto every span, so the total is what multiplies."""
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    for index in range(4):
        resource_spans.resource.attributes.add(key=f"pad_{index}").value.string_value = "v" * (
            MAX_RESOURCE_ATTRIBUTES // 2
        )
    resource_spans.scope_spans.add().spans.add(**span())

    with pytest.raises(AttributeTooLarge, match="bytes of attributes"):
        decode(request.SerializeToString(), RESOURCE)


def _varint(value: int) -> bytes:
    out = b""
    while True:
        part = value & 0x7F
        value >>= 7
        out += bytes([part | (0x80 if value else 0)])
        if not value:
            return out


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def test_repeated_resource_fields_are_capped_once_merged():
    """`resource` is singular, so protobuf merges repeats of it and the attribute
    lists concatenate. Each occurrence can pass every cap and the merge not."""
    occurrences = 8
    chunks = []
    for occurrence in range(occurrences):
        resource = Resource()
        for index in range(MAX_SPAN_ATTRIBUTES // occurrences):
            resource.attributes.add(key=f"o{occurrence}_k{index}").value.string_value = "v" * 900
        raw = resource.SerializeToString()
        assert len(raw) < MAX_RESOURCE_ATTRIBUTES, "each occurrence must pass on its own"
        chunks.append(raw)

    scope = ScopeSpans()
    scope.spans.add(**span())
    inner = b"".join(_field(1, chunk) for chunk in chunks) + _field(2, scope.SerializeToString())

    with pytest.raises(AttributeTooLarge, match="bytes of attributes"):
        decode(_field(1, inner), RESOURCE)


def test_a_body_that_is_not_a_trace_request_is_refused():
    with pytest.raises(TraceError):
        decode(b"\xff\xff nonsense \xff", RESOURCE)


def test_the_route_stores_what_it_decoded(client, provider):
    response = client.post(PATH, content=exported(({"service.name": "vllm"}, [span()])))

    assert response.status_code == 200
    assert provider.written[0]["trace_id"] == TRACE.hex()
    assert provider.written[0]["resource_id"] == "acme"


def test_an_oversized_body_is_a_413(client):
    body = exported(({}, [span() for _ in range(MAX_SPANS + 1)]))

    assert client.post(PATH, content=body).status_code == 413


def test_a_broken_body_is_a_400_not_a_500(client):
    assert client.post(PATH, content=b"nonsense").status_code == 400


def test_traces_need_a_writer(anonymous):
    assert anonymous.post(PATH, content=b"").status_code == 401
