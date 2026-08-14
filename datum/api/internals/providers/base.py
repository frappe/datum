from __future__ import annotations

from abc import ABC, abstractmethod


class ProviderError(RuntimeError):
    """The store could not be reached. Never swallowed into a silent success."""


class QueryRefused(ProviderError):
    """The store rejected the write. The caller's fault, not the store's."""


class DatumProvider(ABC):
    """What the service needs from a storage engine, and nothing more.

    Subclass it, then point `app.py` at it. One that forgets a method fails at
    construction. Datum writes only: reads go to ClickHouse directly.
    """

    @abstractmethod
    def insert(self, table: str, rows: list[dict], columns: tuple[str, ...]) -> int:
        """Write one batch into `table`. Rows are keyed by column name; `columns` is
        the order they land in. Returns rows accepted."""

    @abstractmethod
    def ping(self) -> bool:
        """Is the store reachable?"""

    @abstractmethod
    def close(self) -> None:
        """Close the connection to the store."""
