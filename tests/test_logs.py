from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from datum import create_app
from datum.config import MAX_LOG_ATTRIBUTES, MAX_LOG_BATCH, MAX_LOG_MESSAGE
from tests.conftest import SETTINGS, FakeProvider, mint

LINE = {
    "ts": "2026-08-05T10:00:00Z",
    "product": "pilot",
    "service": "worker",
    "level": "info",
    "source": "worker_pool.log",
    "message": "job done",
}


def post(client, *lines):
    return client.post("/v1/logs/ingest", json={"lines": list(lines)})


def test_a_batch_is_accepted(client, provider):
    response = post(client, LINE)

    assert response.status_code == 200
    assert response.json() == {"accepted": 1}
    assert len(provider.written) == 1


def test_the_token_decides_the_resource_id(client, provider):
    post(client, {**LINE, "attributes": {"queue": "default"}})

    written = provider.written[0]
    assert written["resource_id"] == "acme"
    assert written["product"] == "pilot"
    assert written["service"] == "worker"
    assert written["level"] == "info"
    assert written["source"] == "worker_pool.log"
    assert written["message"] == "job done"
    assert written["attributes"] == {"queue": "default"}


def test_a_claimed_resource_id_in_the_attributes_is_dropped(client, provider):
    """The token is the only thing that can say who a row belongs to."""
    post(client, {**LINE, "attributes": {"resource_id": "someone_else"}})

    written = provider.written[0]
    assert written["resource_id"] == "acme"
    assert "resource_id" not in written["attributes"]


def as_client(tokens, claims):
    """A client carrying one specific set of claims."""
    app = create_app(SETTINGS, tokens=tokens, provider=FakeProvider())
    headers = {"Authorization": f"Bearer {mint(claims)}"}
    return TestClient(app, headers=headers)


def test_a_reader_cannot_write_logs(tokens):
    with as_client(tokens, {"resource_id": "acme", "access": ["read"]}) as client:
        response = post(client, LINE)

    assert response.status_code == 403
    assert "write" in response.json()["detail"]


def test_a_token_without_a_resource_id_is_not_an_identity(tokens):
    with as_client(tokens, {"access": ["read", "write"]}) as client:
        response = post(client, LINE)

    assert response.status_code == 401


def test_log_ingest_is_behind_the_same_gate(anonymous):
    assert post(anonymous, LINE).status_code == 401


def test_an_empty_log_batch_is_refused(client):
    assert client.post("/v1/logs/ingest", json={"lines": []}).status_code == 422


@pytest.mark.parametrize(
    "line",
    [
        {**LINE, "product": "not a product"},
        {**LINE, "service": "not a service"},
        {**LINE, "level": "not a level"},
        {**LINE, "source": "not a source"},
        {**LINE, "message": 12},
        {"product": "pilot", "service": "worker", "level": "info", "source": "x", "message": "y"},
    ],
)
def test_an_unusable_log_line_is_a_422(client, line):
    assert post(client, line).status_code == 422


def test_a_log_line_over_the_attribute_cap_is_refused(client):
    wide = {
        **LINE,
        "attributes": {f"key_{index}": "v" for index in range(MAX_LOG_ATTRIBUTES + 1)},
    }
    assert post(client, wide).status_code == 422


def test_a_log_line_of_exactly_the_attribute_cap_is_accepted(client):
    fits = {
        **LINE,
        "attributes": {f"key_{index}": "v" for index in range(MAX_LOG_ATTRIBUTES)},
    }
    assert post(client, fits).status_code == 200


def test_a_message_at_the_length_cap_is_accepted(client):
    fits = {**LINE, "message": "x" * MAX_LOG_MESSAGE}
    assert post(client, fits).status_code == 200


def test_a_message_over_the_length_cap_is_refused(client):
    over = {**LINE, "message": "x" * (MAX_LOG_MESSAGE + 1)}
    assert post(client, over).status_code == 422


def test_a_log_batch_over_the_cap_is_refused(client):
    lines = [LINE for _ in range(MAX_LOG_BATCH + 1)]
    assert post(client, *lines).status_code == 422


def test_a_log_batch_at_the_cap_is_accepted(client):
    lines = [LINE for _ in range(MAX_LOG_BATCH)]
    assert post(client, *lines).status_code == 200
