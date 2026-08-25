from fastapi import APIRouter

from datum.api.routes.v1 import ingest, logs, resource, traces

router = APIRouter(prefix="/v1")
router.include_router(ingest.router)
router.include_router(logs.router)
router.include_router(traces.router)
router.include_router(resource.resource_router)

__all__ = ["router"]
