from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import jwt

ACCESS_CLAIM = "access"
ADMIN_CLAIM = "admin"
WRITE = "write"

ADMIN_CALLER = "admin"
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]

PUBLIC_KEY_PATH_VARIABLE = "DATUM_JWT_PUBLIC_KEY_FILE"
OIDC_ISSUER_VARIABLE = "DATUM_OIDC_ISSUER"

DISCOVERY_PATH = "/.well-known/openid-configuration"
KEY_LIFESPAN = 300
DISCOVERY_TIMEOUT = 5.0


@dataclass(frozen=True)
class Identity:
    """Who a token speaks for, and what it may do.

    Central signs bench and site logins with the same key, so a token claiming
    no access gets nothing here. `read` is kept but no longer served.
    """

    resource_id: str = ""
    access: frozenset[str] = frozenset()
    is_admin: bool = False

    @property
    def caller(self) -> str:
        """Admins share one budget: they name no resource."""
        return ADMIN_CALLER if self.is_admin else self.resource_id

    @property
    def can_write(self) -> bool:
        """Every row is stamped with `resource_id`, so a token without one cannot write."""
        return WRITE in self.access and bool(self.resource_id)

    @classmethod
    def from_claims(cls, claims: dict) -> Identity:
        """A machine names itself; an admin names the fleet, so it may carry none."""
        resource_id = claims.get("resource_id")
        is_admin = claims.get(ADMIN_CLAIM) is True

        if not resource_id and not is_admin:
            raise jwt.InvalidTokenError("Token carries no resource_id.")

        access = claims.get(ACCESS_CLAIM) or ()

        if isinstance(access, str):
            access = access.split()

        return cls(
            resource_id=str(resource_id or ""),
            access=frozenset(str(entry) for entry in access),
            is_admin=is_admin,
        )


class TokenVerifier:
    """Verifies the JWTs Central mints.

    Either a public key on disk or an OIDC issuer to fetch one from. HMAC is
    deliberately absent: RSA and ECDSA only.
    """

    def __init__(self, public_key: str | None = None, oidc_issuer: str | None = None):
        self.public_key = (public_key or "").strip()
        self.oidc_issuer = (oidc_issuer or "").strip().rstrip("/")
        self._keys: jwt.PyJWKClient | None = None

    @classmethod
    def from_env(cls) -> TokenVerifier:
        """A key path that is set but unreadable is a startup failure, never a
        service that silently answers 401 to everyone."""
        location = os.environ.get(PUBLIC_KEY_PATH_VARIABLE)
        issuer = os.environ.get(OIDC_ISSUER_VARIABLE)
        if not location:
            return cls(oidc_issuer=issuer)

        path = Path(location)
        if not path.is_file():
            raise RuntimeError(f"{PUBLIC_KEY_PATH_VARIABLE} is {location}, which is not a file.")
        return cls(public_key=path.read_text(), oidc_issuer=issuer)

    @property
    def is_configured(self) -> bool:
        return bool(self.public_key or self.oidc_issuer)

    def resolve(self, token: str) -> Identity | None:
        """The identity a valid token carries, or None. Never raises."""
        if not self.is_configured:
            return None
        try:
            return Identity.from_claims(self._decode(token))
        except (jwt.InvalidTokenError, jwt.PyJWKClientError):
            return None

    def _decode(self, token: str) -> dict:
        if not self.oidc_issuer:
            return jwt.decode(
                token, self.public_key, algorithms=ALGORITHMS, options={"verify_aud": False}
            )
        signing_key = self._signing_key(token)
        return jwt.decode(
            token,
            signing_key,
            algorithms=ALGORITHMS,
            issuer=self.oidc_issuer,
            options={"verify_aud": False},
        )

    def _signing_key(self, token: str):
        """The key the token's `kid` names, from the issuer's JWKS."""
        if self._keys is None:
            self._keys = jwt.PyJWKClient(self._jwks_uri(), lifespan=KEY_LIFESPAN)
        return self._keys.get_signing_key_from_jwt(token).key

    def _jwks_uri(self) -> str:
        """Discovery, then the key set.

        Failure raises `PyJWKClientError`, so `resolve` answers 401 while the
        provider is down rather than serving reads with no key at all.
        """
        url = f"{self.oidc_issuer}{DISCOVERY_PATH}"
        try:
            with urllib.request.urlopen(url, timeout=DISCOVERY_TIMEOUT) as response:
                document = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as unreachable:
            raise jwt.PyJWKClientError(f"{url} could not be read: {unreachable}") from unreachable

        uri = document.get("jwks_uri")
        if not uri:
            raise jwt.PyJWKClientError(f"{url} carries no jwks_uri")
        return uri
