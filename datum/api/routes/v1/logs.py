from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter

from datum.api.dependencies import Provider, Writer, rate_limit
from datum.api.internals.schemas import IngestResponse, LogLine, LogLineRequest

router = APIRouter(tags=["logs"])

TABLE = "logs"
LOG_COLUMNS = (
    "ts",
    "resource_id",
    "product",
    "service",
    "level",
    "source",
    "message",
    "attributes",
)


@router.post("/logs/ingest")
def ingest(
    request: LogLineRequest,
    provider: Provider,
    resource_id: Writer,
    _: Annotated[None, rate_limit(12, 60)],
) -> IngestResponse:
    """Stamp every line with the token's resource_id, then write the batch."""
    lines = get_rows(request.lines, resource_id)
    return IngestResponse(accepted=provider.insert(TABLE, lines, LOG_COLUMNS))


def get_rows(lines: list[LogLine], resource_id: str) -> list[dict]:
    return [line.get_row(resource_id) for line in lines]
