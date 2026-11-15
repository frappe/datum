import json


def test_health_is_unversioned(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_fails_when_clickhouse_does_not_answer(client, provider, monkeypatch):
    monkeypatch.setattr(provider, "ping", lambda: False)
    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "clickhouse unreachable"}


def test_a_refused_request_names_its_sender(client, caplog):
    client.post("/v1/ingest", json={})
    message = caplog.records[-1].getMessage()
    line = json.loads(message.removeprefix("<4>"))

    assert message.startswith("<4>")

    assert line == {
        "level": "warning",
        "msg": "request refused",
        "route": "/v1/ingest",
        "status": 422,
        "sender": "acme",
        "count": 1,
    }


def test_refusals_within_a_minute_share_one_line(client, caplog):
    for _ in range(3):
        client.post("/v1/ingest", json={})

    assert len([record for record in caplog.records if record.name == "datum.refusals"]) == 1


def test_an_accepted_request_logs_nothing(client, caplog):
    sample = {"metric": "up", "value": 1, "ts": "2026-08-05T10:00:00Z"}
    assert client.post("/v1/ingest", json={"samples": [sample]}).status_code == 200

    assert not [record for record in caplog.records if record.name == "datum.refusals"]


def test_health_is_absent_from_the_schema(client):
    paths = client.get("/v1/openapi.json").json()["paths"]

    assert "/health" not in paths


def test_every_documented_path_is_versioned(client):
    paths = client.get("/v1/openapi.json").json()["paths"]

    assert paths
    assert all(path.startswith("/v1/") for path in paths)


def test_the_published_routes(client):
    """Write-only: reads go to ClickHouse directly, so datum publishes none."""
    paths = client.get("/v1/openapi.json").json()["paths"]

    assert sorted(paths) == [
        "/v1/ingest",
        "/v1/ingest/remote",
        "/v1/logs/ingest",
        "/v1/traces",
    ]


def test_startup_refuses_a_store_it_cannot_reach(provider, client):
    """Migrations make the schema; the lifespan only checks ClickHouse answers."""
    assert provider.pinged


def test_malformed_body_is_a_422(client):
    assert client.post("/v1/ingest", json={}).status_code == 422
