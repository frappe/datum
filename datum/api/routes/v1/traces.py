from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Response, status

from datum.api.dependencies import Provider, Writer, rate_limit
from datum.api.internals.traces import decode
from datum.api.internals.wire import CONTENT_TYPE

router = APIRouter(tags=["traces"])

TABLE = "traces"
TRACE_COLUMNS = (
    "ts",
    "resource_id",
    "service",
    "span_name",
    "span_kind",
    "trace_id",
    "span_id",
    "parent_span_id",
    "duration_ns",
    "status_code",
    "status_message",
    "attributes",
)


@router.post("/traces")
def ingest(
    body: Annotated[bytes, Body(media_type=CONTENT_TYPE)],
    provider: Provider,
    resource_id: Writer,
    _: Annotated[None, rate_limit(12, 60)],
) -> Response:
    """OTLP/HTTP. A collector retries anything that is not 2xx, so the empty
    ExportTraceServiceResponse is the success it expects."""
    provider.insert(TABLE, decode(body, resource_id), TRACE_COLUMNS)
    return Response(content=b"", media_type=CONTENT_TYPE, status_code=status.HTTP_200_OK)
