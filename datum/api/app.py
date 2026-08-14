from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from datum.api import errors
from datum.api.internals import ClickHouseProvider, DatumProvider, TokenVerifier
from datum.api.limiter import RateLimiter
from datum.api.routes import router
from datum.config import Settings

TITLE = "datum"
VERSION = "0.1.0"

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

    `tokens` defaults to the public key in `DATUM_JWT_PUBLIC_KEY`. With none
    set, every /v1 call is a 401.
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

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        """Liveness probe. Unversioned, so a future /v2 never breaks it."""
        return {"status": "ok"}

    return app
