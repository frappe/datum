from __future__ import annotations

import os
import re
from dataclasses import dataclass

from datum.config.limits import TIMEOUT

IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Fixed, not configurable: the migrations name it too, and two sources would drift.
DATABASE = "datum"


@dataclass(frozen=True)
class Settings:
    """How the API reaches ClickHouse. The user needs INSERT, nothing more."""

    host: str
    port: int = 8123
    database: str = DATABASE
    username: str = "default"
    password: str = ""
    timeout: float = TIMEOUT

    @classmethod
    def check_identifier(cls, name: str, what: str) -> str:
        """Checked here, so no construction path puts an unquotable name into SQL."""
        if not IDENTIFIER.match(name):
            raise ValueError(f"{what} must match {IDENTIFIER.pattern}, not {name!r}")
        return name

    @classmethod
    def from_env(cls) -> Settings:
        host = os.environ.get("DATUM_CLICKHOUSE_HOST")
        if not host:
            raise RuntimeError("DATUM_CLICKHOUSE_HOST is not set; point it at ClickHouse.")

        return cls(
            host=host,
            port=int(os.environ.get("DATUM_CLICKHOUSE_PORT", cls.port)),
            username=os.environ.get("DATUM_CLICKHOUSE_USER", cls.username),
            password=os.environ.get("DATUM_CLICKHOUSE_PASSWORD", cls.password),
            timeout=float(os.environ.get("DATUM_TIMEOUT", cls.timeout)),
        )
