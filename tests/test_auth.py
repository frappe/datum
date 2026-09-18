import time

from datum.api.internals import Identity, TokenVerifier
from tests.conftest import CLAIMS, IDENTITY, JWKS_URL, REGION_ID, TOKEN, mint, tamper


def test_a_signed_token_resolves_to_its_identity(tokens):
    assert tokens.resolve(TOKEN) == IDENTITY


def test_a_token_signed_by_someone_else_is_refused(tokens):
    """A key that is not on the published set, under the key id of one that is."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other = (
        Ed25519PrivateKey.generate()
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )

    assert tokens.resolve(mint(key=other)) is None


def test_a_tampered_signature_is_refused(tokens):
    assert tokens.resolve(tamper(TOKEN)) is None


def test_an_expired_token_is_refused(tokens):
    stale = mint({"exp": int(time.time()) - 60, **CLAIMS})

    assert tokens.resolve(stale) is None


def test_nonsense_is_refused_rather_than_raised(tokens):
    assert tokens.resolve("not-a-jwt") is None


def test_without_a_key_nothing_resolves():
    assert TokenVerifier().resolve(TOKEN) is None


def test_the_identity_is_the_resource_id_and_what_it_may_do():
    identity = Identity.from_claims({"resource_id": "acme", "access": ["read", "write"]})

    assert identity.resource_id == "acme"
    assert identity.can_write is True
    assert identity.access == frozenset({"read", "write"})


def test_access_may_be_written_as_one_string():
    claims = {"resource_id": "acme", "access": "read write"}

    assert Identity.from_claims(claims).access == frozenset({"read", "write"})


def test_a_token_claiming_no_access_may_do_nothing():
    identity = Identity.from_claims({"resource_id": "acme"})

    assert identity.can_write is False


def test_a_read_claim_is_kept_but_grants_nothing():
    """Central still mints `read`; datum serves no reads, and does not refuse it."""
    reader = Identity.from_claims({"resource_id": "acme", "access": ["read"]})

    assert reader.can_write is False
    assert "read" in reader.access


def test_v1_refuses_an_anonymous_caller(anonymous):
    response = anonymous.post("/v1/ingest", json={"samples": []})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_v1_refuses_an_unknown_token(anonymous):
    response = anonymous.post(
        "/v1/ingest", json={"samples": []}, headers={"Authorization": "Bearer wrong"}
    )

    assert response.status_code == 401


def test_health_stays_open(anonymous):
    assert anonymous.get("/health").status_code == 200


def test_auth_runs_before_validation(anonymous):
    """A bad body from a stranger is a 401, never a 422 describing our schema."""
    response = anonymous.post("/v1/ingest", json={"nonsense": True})

    assert response.status_code == 401


def test_a_token_without_a_resource_id_is_no_identity_at_all(tokens):
    """Every row is stamped with it, so a token without one has nothing
    to address."""
    assert tokens.resolve(mint({"access": ["read", "write"]})) is None


def test_a_key_set_and_a_region_are_both_needed():
    """A key set without a region would take any region's token."""
    assert TokenVerifier(jwks_url=JWKS_URL, region_id=REGION_ID).is_configured is True
    assert TokenVerifier(jwks_url=JWKS_URL).is_configured is False
    assert TokenVerifier(region_id=REGION_ID).is_configured is False
    assert TokenVerifier().is_configured is False


def test_a_token_addressed_to_another_region_is_refused(tokens):
    """One region's key set is every region's key set, so the audience is the only thing
    keeping a pilot elsewhere in the fleet from writing here."""
    elsewhere = mint({**CLAIMS, "aud": "atlas-datum:9"})

    assert tokens.resolve(elsewhere) is None


def test_a_token_addressed_to_this_region_is_accepted(tokens):
    assert tokens.resolve(mint({**CLAIMS, "aud": f"atlas-datum:{REGION_ID}"})) == IDENTITY


def test_a_token_addressed_to_nothing_is_refused(tokens):
    """The pilot_credential_id this used to carry names no host."""
    assert tokens.resolve(mint({**CLAIMS, "aud": "pilot-123"})) is None


def test_an_admin_names_the_fleet_not_a_machine():
    """Central manages resources it did not create samples for, so it carries no
    resource_id of its own."""
    identity = Identity.from_claims({"admin": True, "access": ["write"]})

    assert identity.is_admin is True
    assert identity.resource_id == ""


def test_a_non_admin_still_must_name_itself():
    assert Identity.from_claims({"access": ["write"]}) is None


def test_an_admin_naming_no_machine_cannot_write():
    """It would stamp an empty resource_id on every row."""
    identity = Identity.from_claims({"admin": True, "access": ["write"]})

    assert identity.can_write is False


def test_an_admin_that_also_names_a_machine_can_write():
    identity = Identity.from_claims({"admin": True, "resource_id": "acme", "access": ["write"]})

    assert (identity.can_write, identity.is_admin) == (True, True)


def test_admin_is_the_boolean_not_a_truthy_string():
    """A token claiming the string "false" must not become an admin."""
    identity = Identity.from_claims({"resource_id": "acme", "admin": "false"})

    assert identity.is_admin is False


def test_a_token_claiming_no_admin_is_not_one():
    assert Identity.from_claims({"resource_id": "acme"}).is_admin is False


def test_a_machine_is_billed_as_itself():
    assert Identity.from_claims({"resource_id": "acme"}).caller == "acme"


def test_an_admin_is_billed_to_one_shared_budget():
    """It names no machine, so there is nothing else to key on."""
    assert Identity.from_claims({"admin": True}).caller == "admin"


def test_an_admin_that_names_a_machine_is_still_billed_as_an_admin():
    """The claim decides, not the absence of a resource_id."""
    claims = {"admin": True, "resource_id": "acme"}

    assert Identity.from_claims(claims).caller == "admin"
