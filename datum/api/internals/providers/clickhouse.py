from __future__ import annotations

import clickhouse_connect
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError

from datum.api.internals.providers.base import DatumProvider, ProviderError, QueryRefused
from datum.config.api import DATABASE


class ClickHouseProvider(DatumProvider):
    """Writes rows into whichever table the caller names. Nothing here reads them back."""

    def __init__(
        self,
        host: str,
        database: str = DATABASE,
        port: int = 8123,
        username: str = "default",
        password: str = "",
        timeout: float = 30.0,
    ):
        self.database = database
        self.timeout = timeout
        self._connection = {
            "host": host,
            "port": port,
            "username": username,
            "password": password,
        }
        self._client = None

    @property
    def client(self):
        """Connected on first use, so building the provider opens no socket."""
        if self._client is None:
            self._client = clickhouse_connect.get_client(
                **self._connection,
                connect_timeout=self.timeout,
                send_receive_timeout=self.timeout,
            )
        return self._client

    def ping(self) -> bool:
        """Answers False rather than raising: connecting is itself what may fail."""
        try:
            return self.client.ping()
        except ClickHouseError:
            return False

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def insert(self, table: str, rows: list[dict], columns: tuple[str, ...]) -> int:
        if not rows:
            return 0
        data = [[row[column] for column in columns] for row in rows]
        self._run(
            self.client.insert,
            table,
            data,
            column_names=list(columns),
            database=self.database,
        )

        return len(rows)

    def _run(self, call, *arguments, **keywords):
        """Unreachable is a 503, refused is a 400. Never a silent success."""
        try:
            return call(*arguments, **keywords)
        except OperationalError as unreachable:
            raise ProviderError(f"ClickHouse is unreachable: {unreachable}") from unreachable
        except ClickHouseError as refused:
            raise QueryRefused(f"ClickHouse refused it: {refused}") from refused
