from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Header, HTTPException, Response, status

from datum.api.dependencies import Provider, Writer, rate_limit
from datum.api.internals.remote import decode, is_version_two
from datum.api.internals.schemas import IngestResponse, Sample, SamplesRequest
from datum.api.internals.wire import CONTENT_TYPE

router = APIRouter(tags=["ingest"])

TABLE = "samples"
COLUMNS = ("ts", "metric", "resource_id", "labels", "value")


@router.post("/ingest")
def ingest(
    body: SamplesRequest,
    provider: Provider,
    resource_id: Writer,
    _: Annotated[None, rate_limit(12, 60)],  # 1 request every 5 seconds
) -> IngestResponse:
    """Stamp every row with the token's resource_id, then write the batch."""
    rows = get_rows(body.samples, resource_id)
    return IngestResponse(accepted=provider.insert(TABLE, rows, COLUMNS))


@router.post("/ingest/remote", status_code=status.HTTP_204_NO_CONTENT)
def remote(
    body: Annotated[bytes, Body(media_type=CONTENT_TYPE)],
    provider: Provider,
    resource_id: Writer,
    _: Annotated[None, rate_limit(12, 60)],  # 1 request every 5 seconds
    content_type: str = Header(default=CONTENT_TYPE),
) -> Response:
    """Prometheus remote write. Same rules, a different wrapper.

    Sync like every other route: writing blocks, so it belongs in the threadpool
    rather than on the event loop. 204 with no body, because that is what a
    remote write client expects and retries on anything else.
    """
    if is_version_two(content_type):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Remote write v2 is not read here; send v1.",
        )
    provider.insert(TABLE, decode(body, resource_id), COLUMNS)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def get_rows(samples: list[Sample], resource_id: str) -> list[dict]:
    return [sample.get_row(resource_id) for sample in samples]
