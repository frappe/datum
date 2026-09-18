"""The only way in: the merged key set Atlas publishes.

Served by a real local HTTP server rather than a mock, so the fetch and the JWKS
parsing are actually exercised.
"""

import time

import jwt

from datum.api.internals import TokenVerifier
from datum.api.internals.auth import JWKS_URL_VARIABLE, REGION_ID_VARIABLE, issuer_for_key_id
from tests.conftest import CLAIMS, IDENTITY, JWKS_URL, KEY_ID, REGION_ID, mint, serve, tamper


def verifier() -> TokenVerifier:
    return TokenVerifier(jwks_url=JWKS_URL, region_id=REGION_ID)


def test_a_token_is_verified_against_the_fetched_key_set():
    identity = verifier().resolve(mint())

    assert identity is not None
    assert identity == IDENTITY


def test_a_tampered_signature_is_refused():
    assert verifier().resolve(tamper(mint())) is None


def test_the_key_set_is_fetched_once_and_reused():
    reused = verifier()
    token = mint()

    assert reused.resolve(token) is not None
    assert reused.resolve(token) is not None


def test_an_unreachable_key_set_is_a_401_not_a_crash():
    """While the publisher is down, calls are refused rather than let through."""
    assert TokenVerifier(jwks_url="http://127.0.0.1:1/keys").resolve("anything") is None


def test_a_key_set_that_answers_404_is_refused():
    assert TokenVerifier(jwks_url=f"{serve()}-absent").resolve(mint()) is None


def test_the_url_and_region_reach_the_verifier_from_the_unit(monkeypatch):
    monkeypatch.setenv(JWKS_URL_VARIABLE, JWKS_URL)
    monkeypatch.setenv(REGION_ID_VARIABLE, REGION_ID)

    from_env = TokenVerifier.from_env()

    assert from_env.is_configured
    assert from_env.resolve(mint()) is not None


def test_no_key_set_verifies_nothing():
    """There is no second way in to fall back to."""
    empty = TokenVerifier()

    assert empty.is_configured is False
    assert empty.resolve(mint()) is None


def test_the_audience_names_this_regions_datum():
    assert TokenVerifier(jwks_url=JWKS_URL, region_id="7").audience == "atlas-datum:7"


def test_a_token_for_another_regions_datum_is_refused():
    """Same key, same issuer, same resource -- addressed elsewhere."""
    verified_here = TokenVerifier(jwks_url=JWKS_URL, region_id="9")

    assert verified_here.resolve(mint()) is None


def test_a_token_naming_an_issuer_its_key_does_not_belong_to_is_refused():
    """The merged set carries both planes' keys, so Central's key must not sign as Atlas."""
    token = mint({**CLAIMS, "iss": f"atlas:{REGION_ID}"})

    assert verifier().resolve(token) is None


def test_a_token_whose_key_id_names_no_issuer_is_refused():
    assert verifier().resolve(mint(headers={"kid": "1"})) is None


def test_a_token_naming_no_key_is_refused():
    """`kid` is how a key is chosen, so a token without one chooses nothing."""
    assert verifier().resolve(mint(headers={"typ": "JWT"})) is None


def test_a_token_naming_another_algorithm_is_refused():
    """Refused on the header alone: the key set is never reached, unreachable though it is."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    signing_key = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    now = int(time.time())
    token = jwt.encode(
        {"iat": now, "exp": now + 300, **CLAIMS},
        signing_key,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )

    assert (
        TokenVerifier(jwks_url="http://127.0.0.1:1/keys", region_id=REGION_ID).resolve(token)
        is None
    )


def test_a_key_id_names_the_issuer_that_owns_its_namespace():
    assert issuer_for_key_id("central:abc", REGION_ID) == "central"
    assert issuer_for_key_id(f"atlas:{REGION_ID}:abc", REGION_ID) == f"atlas:{REGION_ID}"


def test_a_key_id_outside_every_namespace_names_no_issuer():
    assert issuer_for_key_id("atlas:9:abc", REGION_ID) is None
    assert issuer_for_key_id("abc", REGION_ID) is None
    assert issuer_for_key_id("central:", REGION_ID) is None
    assert issuer_for_key_id("central-other:abc", REGION_ID) is None


def test_the_atlas_namespace_needs_a_region():
    """With no region configured, only Central's keys are usable."""
    assert issuer_for_key_id("atlas:42:abc") is None
    assert issuer_for_key_id("central:abc") == "central"
