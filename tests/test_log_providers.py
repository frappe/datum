from __future__ import annotations

from datetime import UTC, datetime

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from datum.api.internals.providers import (
    ClickHouseProvider,
    ProviderError,
    QueryRefused,
)
from datum.api.routes.v1 import logs


class FakeClient:
    """The driver calls the provider makes, and what it was asked."""

    def __init__(self, error=None):
        self.error = error
        self.inserts: list[tuple] = []

    def insert(self, table, data, column_names, database):
        if self.error:
            raise self.error
        self.inserts.append((table, data, column_names, database))


def build(client=None, **options) -> ClickHouseProvider:
    provider = ClickHouseProvider(host="localhost", **options)
    provider._client = client if client is not None else FakeClient()
    return provider


ROW = {
    "ts": datetime(2026, 8, 5, tzinfo=UTC),
    "resource_id": "acme",
    "product": "pilot",
    "service": "worker",
    "level": "info",
    "source": "worker_pool.log",
    "message": "job done",
    "attributes": {"queue": "default"},
}


def test_the_logs_route_writes_the_logs_table():
    """Logs are a table the route names, not a second provider."""
    assert logs.TABLE == "logs"


def test_a_log_batch_is_written_in_column_order():
    client = FakeClient()

    assert build(client).insert(logs.TABLE, [ROW], logs.LOG_COLUMNS) == 1
    table, data, columns, database = client.inserts[0]
    assert (table, database) == ("logs", "datum")
    assert columns == [
        "ts",
        "resource_id",
        "product",
        "service",
        "level",
        "source",
        "message",
        "attributes",
    ]
    assert data == [
        [
            ROW["ts"],
            "acme",
            "pilot",
            "worker",
            "info",
            "worker_pool.log",
            "job done",
            {"queue": "default"},
        ]
    ]


def test_an_empty_log_batch_touches_the_store_not_at_all():
    client = FakeClient()

    assert build(client).insert(logs.TABLE, [], logs.LOG_COLUMNS) == 0
    assert client.inserts == []


def test_an_unreachable_log_store_is_not_a_refusal():
    client = FakeClient(error=OperationalError("connection refused"))

    with pytest.raises(ProviderError, match="unreachable"):
        build(client).insert(logs.TABLE, [ROW], logs.LOG_COLUMNS)


def test_a_refused_log_write_is_the_callers_fault():
    client = FakeClient(error=DatabaseError("unknown column"))

    with pytest.raises(QueryRefused, match="refused"):
        build(client).insert(logs.TABLE, [ROW], logs.LOG_COLUMNS)


def test_the_logs_route_issues_no_ddl():
    """Migrations create the table; the route only writes."""
    assert not hasattr(logs, "ensure_schema")
