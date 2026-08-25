def test_health_is_unversioned(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
        "/v1/resource/add",
        "/v1/resource/{resource_id}",
        "/v1/resource/{resource_id}/status",
        "/v1/traces",
    ]


def test_startup_refuses_a_store_it_cannot_reach(provider, client):
    """Migrations make the schema; the lifespan only checks ClickHouse answers."""
    assert provider.pinged


def test_malformed_body_is_a_422(client):
    assert client.post("/v1/ingest", json={}).status_code == 422
