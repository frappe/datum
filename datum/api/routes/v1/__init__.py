from fastapi import APIRouter

from datum.api.routes.v1 import ingest, logs, traces

router = APIRouter(prefix="/v1")
router.include_router(ingest.router)
router.include_router(logs.router)
router.include_router(traces.router)

__all__ = ["router"]
