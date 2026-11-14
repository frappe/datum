from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from datum.api import errors
from datum.api.internals import ClickHouseProvider, DatumProvider, TokenVerifier
from datum.api.limiter import RateLimiter
from datum.api.routes import router
from datum.config import Settings

TITLE = "datum"
VERSION = "0.1.0"

# One JSON line on stderr for each refused /v1 request. The `<N>` prefix gives journald its
# syslog priority, so the Datum agent sends it with the warnings, without the access log.
refusals = logging.getLogger("datum.refusals")
refusals.propagate = False
if not refusals.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    refusals.addHandler(handler)
    refusals.setLevel(logging.INFO)

TAGS = [
    {"name": "ingest", "description": "Write samples. The token decides who they belong to."},
    {"name": "logs", "description": "Write log lines. The token decides who they belong to."},
]


def get_provider(settings: Settings) -> DatumProvider:
    return ClickHouseProvider(
        host=settings.host,
        port=settings.port,
        username=settings.username,
        password=settings.password,
        database=settings.database,
        timeout=settings.timeout,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.provider = app.state.provider or get_provider(app.state.settings)
    assert isinstance(app.state.provider, DatumProvider)

    if not app.state.provider.ping():
        raise RuntimeError("ClickHouse is not reachable; ensure migrations have run.")

    yield
    app.state.provider.close()


def create_app(
    settings: Settings | None = None,
    tokens: TokenVerifier | None = None,
    provider: DatumProvider | None = None,
) -> FastAPI:
    """Build the `datum-api` application.

    `tokens` defaults to the key set in `DATUM_JWKS_URL`. With none set, every
    /v1 call is a 401.
    """
    app = FastAPI(
        title=TITLE,
        version=VERSION,
        openapi_tags=TAGS,
        openapi_url="/v1/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings or Settings.from_env()
    app.state.tokens = tokens or TokenVerifier.from_env()
    app.state.provider = provider
    app.state.limiter = RateLimiter()
    errors.install(app)
    app.include_router(router)

    @app.middleware("http")
    async def log_refusal(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            log_refused_request(request, 500)
            raise
        if response.status_code >= 400 and request.url.path.startswith("/v1/"):
            log_refused_request(request, response.status_code)
        return response

    @app.get("/health", include_in_schema=False)
    def health() -> JSONResponse:
        """Unversioned, so a future /v2 never breaks it. A 503 when ClickHouse does not answer,
        because then every write fails."""
        if not app.state.provider.ping():
            return JSONResponse(status_code=503, content={"status": "clickhouse unreachable"})
        return JSONResponse(content={"status": "ok"})

    return app


def log_refused_request(request: Request, status: int) -> None:
    """A 5xx is a fault of Datum; a 4xx is a fault of the sender, named when its token was valid."""
    level, priority = ("error", 3) if status >= 500 else ("warning", 4)
    line = {
        "level": level,
        "msg": "request refused",
        "route": request.url.path,
        "status": status,
        "sender": getattr(request.state, "resource_id", ""),
    }
    refusals.info(f"<{priority}>{json.dumps(line)}")
