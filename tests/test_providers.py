from datetime import UTC, datetime

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from datum.api.internals.providers import (
    ClickHouseProvider,
    DatumProvider,
    ProviderError,
    QueryRefused,
)
from datum.api.routes.v1 import ingest, resource


class FakeClient:
    """The driver calls the provider makes, and what it was asked."""

    def __init__(self, error=None, reachable=True):
        self.error = error
        self.reachable = reachable
        self.inserts: list[tuple] = []
        self.closed = False

    def insert(self, table, data, column_names, database):
        if self.error:
            raise self.error
        self.inserts.append((table, data, column_names, database))

    def ping(self):
        if self.error:
            raise self.error
        return self.reachable

    def close(self):
        self.closed = True


def build(client=None, **options) -> ClickHouseProvider:
    provider = ClickHouseProvider(host="localhost", **options)
    provider._client = client if client is not None else FakeClient()
    return provider


ROW = {
    "ts": datetime(2026, 8, 5, tzinfo=UTC),
    "metric": "cpu",
    "resource_id": "acme",
    "labels": {"region": "ap_south_1"},
    "value": 12.5,
}


def test_a_provider_missing_a_method_cannot_be_built():
    class Half(DatumProvider):
        def insert(self, table, rows, columns):
            return 0

    with pytest.raises(TypeError, match="abstract"):
        Half()


def test_building_a_provider_opens_no_connection():
    assert ClickHouseProvider(host="localhost")._client is None


def test_a_provider_exposes_no_way_to_read():
    """Reads are Insights' job, direct. A read method here would be a second door."""
    for absent in ("fetch", "get_metrics", "get_labels", "get_label_values", "get_columns"):
        assert not hasattr(DatumProvider, absent)


def test_the_provider_issues_no_ddl():
    """Migrations create the schema; datum-api never does."""
    assert not hasattr(DatumProvider, "ensure_schema")


def test_a_batch_is_written_in_the_column_order_it_was_given():
    client = FakeClient()

    assert build(client).insert(ingest.TABLE, [ROW], ingest.COLUMNS) == 1
    table, data, columns, database = client.inserts[0]
    assert (table, database) == ("samples", "datum")
    assert columns == ["ts", "metric", "resource_id", "labels", "value"]
    assert data == [[ROW["ts"], "cpu", "acme", {"region": "ap_south_1"}, 12.5]]


def test_the_caller_names_the_table_not_the_provider():
    client = FakeClient()
    provider = build(client)

    provider.insert(ingest.TABLE, [ROW], ingest.COLUMNS)
    resource.write(provider, "vm-1", "Active")

    assert [insert[0] for insert in client.inserts] == ["samples", "resources"]


def test_a_resource_row_carries_only_its_two_columns():
    """`updated_at` defaults to now(): it is the ReplacingMergeTree version."""
    client = FakeClient()

    resource.write(build(client), "vm-1", "Terminated")

    _, data, columns, _ = client.inserts[0]
    assert columns == ["resource_id", "status"]
    assert data == [["vm-1", "Terminated"]]


def test_an_empty_batch_touches_the_store_not_at_all():
    client = FakeClient()

    assert build(client).insert(ingest.TABLE, [], ingest.COLUMNS) == 0
    assert client.inserts == []


def test_an_unreachable_store_is_not_a_refusal():
    client = FakeClient(error=OperationalError("connection refused"))

    with pytest.raises(ProviderError, match="unreachable"):
        build(client).insert(ingest.TABLE, [ROW], ingest.COLUMNS)


def test_a_refused_write_is_the_callers_fault():
    client = FakeClient(error=DatabaseError("Unknown element 'Nonsense' for enum"))

    with pytest.raises(QueryRefused, match="refused"):
        resource.write(build(client), "vm-1", "Nonsense")


def test_ping_answers_true_when_the_store_is_up():
    assert build(FakeClient()).ping() is True


def test_ping_answers_false_rather_than_raising():
    """The lifespan asks a question; it does not want an exception."""
    assert build(FakeClient(error=OperationalError("refused"))).ping() is False
    assert build(FakeClient(reachable=False)).ping() is False


def test_closing_drops_the_client_so_a_later_call_reconnects():
    client = FakeClient()
    provider = build(client)

    provider.close()

    assert client.closed is True
    assert provider._client is None
