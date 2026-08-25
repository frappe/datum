from __future__ import annotations

import math

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from datum.api.internals import ProviderError, QueryRefused
from datum.api.internals.remote import (
    RemoteWriteError,
    TooManyLabels,
    TooManySamples,
    TooManySeries,
)
from datum.api.internals.traces import (
    AttributeTooLarge,
    TooManyAttributes,
    TooManySpans,
    TraceError,
)
from datum.api.internals.wire import BodyTooLarge

UNPROCESSABLE = 422

# Every failure a caller can cause, and what they are told. Anything absent is a
# bug and becomes a 500 with a traceback, which is what we want. QueryRefused
# comes first: it is a ProviderError, but it is the caller's fault, not the store's.
STATUS = {
    QueryRefused: 400,
    TooManySamples: 413,
    BodyTooLarge: 413,
    TooManySeries: 413,
    TooManyLabels: 413,
    RemoteWriteError: 400,
    TooManySpans: 413,
    TooManyAttributes: 413,
    AttributeTooLarge: 413,
    TraceError: 400,
    NotImplementedError: 501,
    ProviderError: 503,
}


def install(app: FastAPI) -> None:
    """Point every known failure at its status, so routes never catch."""
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    for error in STATUS:
        app.add_exception_handler(error, handle_known_error)


async def handle_known_error(request: Request, error: Exception) -> JSONResponse:
    """The exception message is already written for the caller."""
    for kind, status in STATUS.items():
        if isinstance(error, kind):
            return JSONResponse(status_code=status, content={"detail": str(error)})
    raise error


def serialisable(value):
    """Same structure, with non-finite floats turned into their names."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {key: serialisable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialisable(item) for item in value]
    return value


async def handle_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    """A 422 echoes the input, and NaN is legal in a request but not a response."""
    detail = serialisable(jsonable_encoder(error.errors()))
    return JSONResponse(status_code=UNPROCESSABLE, content={"detail": detail})
