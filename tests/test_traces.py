import cramjam
import pytest

from datum.api.internals.traces import TooManySpans, TraceError, decode
from datum.api.internals.traces.traces_pb2 import ExportTraceServiceRequest
from datum.config import MAX_SPAN_ATTRIBUTES, MAX_SPANS

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
