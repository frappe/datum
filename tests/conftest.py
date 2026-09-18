import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from fastapi.testclient import TestClient

from datum import Settings, create_app
from datum.api.internals import Identity, TokenVerifier
from datum.api.internals.auth import ALGORITHM
from datum.api.internals.providers import DatumProvider

SETTINGS = Settings(host="localhost")

# A fixed throwaway keypair, so tests never generate one. Never used anywhere but here.
PRIVATE_KEY = """\
-----BEGIN PRIVATE KEY-----
MC4CAQAwBQYDK2VwBCIEILkupBggTu21cMflw8H/Il2dONYkt2o94weh5u7gX/gK
-----END PRIVATE KEY-----
"""

PUBLIC_KEY = """\
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAGQQFtXr9RIcyNvvetKH/GRiNPSNQ54Idc0ENA9r+ewQ=
-----END PUBLIC KEY-----
"""


KEY_ID = "central:1"
REGION_ID = "42"


def jwks() -> dict:
    """The merged set Atlas publishes, as far as datum is concerned."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    key = jwt.algorithms.OKPAlgorithm.to_jwk(load_pem_public_key(PUBLIC_KEY.encode()), as_dict=True)
    return {"keys": [{**key, "kid": KEY_ID, "use": "sig", "alg": ALGORITHM}]}


class Publisher(BaseHTTPRequestHandler):
    """Serves the key set, the way Central does."""

    def do_GET(self):
        if self.path == "/keys":
            self._send(jwks())
        else:
            self.send_error(404)

    def _send(self, document: dict) -> None:
        body = json.dumps(document).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(handler: type[BaseHTTPRequestHandler] = Publisher) -> str:
    """Start one key-set server on a free port and return the URL of its key set."""
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/keys"


JWKS_URL = serve()

CLAIMS = {
    "iss": "central",
    "resource_id": "acme",
    "access": ["read", "write"],
    "aud": "test-audience",
}
IDENTITY = Identity(resource_id="acme", access=frozenset({"read", "write"}))


def mint(claims: dict | None = None, key: str | None = None, headers: dict | None = None) -> str:
    """A JWT the way Central would sign one, under the key it publishes.

    The registered claims datum requires are filled in, so a caller passing its own
    `claims` states only what the test is about."""
    now = int(time.time())
    return jwt.encode(
        {"iss": "central", "iat": now, "exp": now + 300, **(claims or CLAIMS)},
        key or PRIVATE_KEY,
        algorithm=ALGORITHM,
        headers=headers or {"kid": KEY_ID},
    )


TOKEN = mint()


def tamper(token: str) -> str:
    """Break the signature for real.

    Flipping the last character is not enough: a 2048-bit RSA signature is 256
    bytes, so base64url's final character carries two significant bits and four
    of them decode to the same signature. Mid-segment characters carry six.
    """
    header, payload, signature = token.split(".")
    index = len(signature) // 2
    swapped = "A" if signature[index] != "A" else "B"
    return f"{header}.{payload}.{signature[:index]}{swapped}{signature[index + 1 :]}"


class FakeProvider(DatumProvider):
    """Stands in for ClickHouse, so route tests open no socket."""

    def __init__(self, **options):
        self.options = options
        self.written: list[dict] = []
        self.resources: list[tuple[str, str]] = []
        self.inserted: list[tuple] = []
        self.pinged = False
        self.closed = False

    def ping(self):
        self.pinged = True
        return True

    def close(self):
        self.closed = True

    def insert(self, table, rows, columns):
        self.inserted.append((table, rows, columns))
        if table == "resources":
            self.resources.extend((row["resource_id"], row["status"]) for row in rows)
        else:
            self.written.extend(rows)
        return len(rows)


@pytest.fixture
def tokens():
    return TokenVerifier(jwks_url=JWKS_URL, region_id=REGION_ID)


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def client(tokens, provider):
    """Authenticated, the way a caller talks to the service."""
    app = create_app(SETTINGS, tokens=tokens, provider=provider)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as test_client:
        yield test_client


@pytest.fixture
def other_tenant(tokens, provider):
    """A second tenant against the same app, so budgets can be told apart."""
    app = create_app(SETTINGS, tokens=tokens, provider=provider)
    token = mint({"resource_id": "other", "access": ["read", "write"]})
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as test_client:
        yield test_client


@pytest.fixture
def anonymous():
    app = create_app(
        SETTINGS,
        tokens=TokenVerifier(),
        provider=FakeProvider(),
    )
    with TestClient(app) as test_client:
        yield test_client
