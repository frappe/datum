from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from datum.api.internals import DatumProvider, Identity
from datum.config.limits import MAX_REQUESTS, RATE_PERIOD

bearer = HTTPBearer(auto_error=False, description="JWT minted by Central.")


def get_provider(request: Request) -> DatumProvider:
    """The provider built at startup. Routes talk to it directly."""
    return request.app.state.provider


def get_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Identity:
    """Resolve the bearer token to who is calling. Gates the whole /v1 mount."""
    identity = credentials and request.app.state.tokens.resolve(credentials.credentials)
    if not identity:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Unknown or missing token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return identity


Caller = Annotated[Identity, Depends(get_identity)]


def rate_limit(limit: int = MAX_REQUESTS, period: float = RATE_PERIOD):
    """Allow route based rate limiting via the token, by returning a Depends called at runtime."""

    def spend(request: Request, identity: Caller) -> None:
        route = request.scope["route"].path
        retry_after = request.app.state.limiter.get_retry_after(
            identity.caller, route, limit, period
        )
        if retry_after:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests.",
                headers={"Retry-After": str(retry_after)},
            )

    return Depends(spend)


def get_admin(identity: Caller) -> Identity:
    """Speaks for the fleet, not for one machine. Only an admin touches resources."""
    if not identity.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Token is not an admin.")
    return identity


def get_writer(identity: Caller) -> str:
    """The resource id stamped on every written row."""
    if not identity.can_write:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Token cannot write.")
    return identity.resource_id


Provider = Annotated[DatumProvider, Depends(get_provider)]
Admin = Annotated[Identity, Depends(get_admin)]
Writer = Annotated[str, Depends(get_writer)]
