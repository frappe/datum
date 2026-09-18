from __future__ import annotations

import os
from dataclasses import dataclass

import jwt

ACCESS_CLAIM = "access"
ADMIN_CLAIM = "admin"
WRITE = "write"

ADMIN_CALLER = "admin"
# Every key on the merged set is Ed25519. Pinning the algorithm keeps a token from
# naming a weaker one and being verified against a key meant for this.
ALGORITHM = "EdDSA"
CENTRAL_ISSUER = "central"

JWKS_URL_VARIABLE = "DATUM_JWKS_URL"
REGION_ID_VARIABLE = "DATUM_REGION_ID"
KEY_LIFESPAN = 300

# `aud` names one region's datum, and is the only thing that keeps another region's
# token out. The machine inside the region is `resource_id`, which `Identity` enforces.
DECODE_OPTIONS = {"verify_aud": True, "require": ["iss", "aud", "iat", "exp"]}


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
    def from_claims(cls, claims: dict) -> Identity | None:
        """A machine names itself; an admin names the fleet, so it may carry none."""
        resource_id = claims.get("resource_id")
        is_admin = claims.get(ADMIN_CLAIM) is True

        if not resource_id and not is_admin:
            return None

        access = claims.get(ACCESS_CLAIM) or ()

        if isinstance(access, str):
            access = access.split()

        return cls(
            resource_id=str(resource_id or ""),
            access=frozenset(str(entry) for entry in access),
            is_admin=is_admin,
        )


def issuer_for_key_id(key_id: str, region_id: str = "") -> str | None:
    """The issuer whose key id namespace this is, or None when no issuer claims it."""
    issuers = [CENTRAL_ISSUER] + ([f"atlas:{region_id}"] if region_id else [])
    for issuer in issuers:
        if key_id.startswith(f"{issuer}:") and key_id.removeprefix(f"{issuer}:"):
            return issuer

    return None


class TokenVerifier:
    """Verifies the JWTs Central mints. Ed25519 only; nothing else is accepted.

    The merged key set at `jwks_url` carries more than one issuer's keys, so a valid
    signature alone does not say who signed. The key id names the issuer, and `iss` is
    held to it. Datum holds no key of its own, so a rotation needs nothing deployed
    here.

    `region_id` is this host's region, and a token must be addressed to it: one region's
    key set is every region's key set, so without the audience a pilot anywhere could
    write here. With either setting missing every call is a 401.
    """

    def __init__(self, jwks_url: str | None = None, region_id: str | None = None):
        self.jwks_url = (jwks_url or "").strip()
        self.region_id = (region_id or "").strip()
        self._keys: jwt.PyJWKClient | None = None

    @classmethod
    def from_env(cls) -> TokenVerifier:
        return cls(
            jwks_url=os.environ.get(JWKS_URL_VARIABLE),
            region_id=os.environ.get(REGION_ID_VARIABLE),
        )

    @property
    def is_configured(self) -> bool:
        """Both, or nothing verifies. A key set without a region would take any region's
        token, which is the hole the audience exists to close."""
        return bool(self.jwks_url and self.region_id)

    @property
    def audience(self) -> str:
        """The audience a token must name to be written here."""
        return f"atlas-datum:{self.region_id}"

    def resolve(self, token: str) -> Identity | None:
        """The identity a valid token carries, or None. Never raises."""
        claims = self.token_claims(token)

        return Identity.from_claims(claims) if claims else None

    def token_claims(self, token: str) -> dict | None:
        """What one token carries, or None when it is not usable.

        Datum verifies against the merged key set it is pointed at and holds no
        verification secret of its own."""
        if not self.is_configured:
            return None

        try:
            header = jwt.get_unverified_header(token)
            key_id = header.get("kid")
            if not isinstance(key_id, str) or header.get("alg") != ALGORITHM:
                return None

            # The key set carries more than one issuer's keys, so a signature alone does
            # not say who signed. The key id does, and `iss` is then held to it.
            issuer = issuer_for_key_id(key_id, self.region_id)
            if issuer is None:
                return None

            # An unknown key id must not make an attacker refetch the key set.
            signing_key = jwt.PyJWKClient.match_kid(self.jwks_client().get_signing_keys(), key_id)
            if signing_key is None or signing_key.algorithm_name != ALGORITHM:
                return None

            return jwt.decode(
                token,
                signing_key.key,
                algorithms=[ALGORITHM],
                audience=self.audience,
                issuer=issuer,
                options=DECODE_OPTIONS,
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError):
            return None

    def jwks_client(self) -> jwt.PyJWKClient:
        """One key set client per verifier: it caches the keys, so nothing refetches
        per request. A set that cannot be read raises, and the call is a 401 rather
        than one let through unverified."""
        if self._keys is None:
            self._keys = jwt.PyJWKClient(self.jwks_url, lifespan=KEY_LIFESPAN)

        return self._keys
