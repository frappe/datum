from __future__ import annotations

import argparse
import os
from pathlib import Path
from string import Template

import clickhouse_connect

DIRECTORY = Path(__file__).parent
UNSAFE = ("'", "\\", ";", "--", "\n", "\r")


def get_migrations() -> list[Path]:
    """Every .sql file here, in name order. The numeric prefix is what orders them."""
    return sorted(DIRECTORY.glob("*.sql"))


def get_statements(sql: str) -> list[str]:
    """One statement per driver call, comments dropped first so a `;` in one cannot split."""
    lines = [line.split("--")[0] for line in sql.splitlines()]
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


def check_password(name: str, value: str) -> str:
    """Refuse rather than escape: these run as the migration user, so a password that
    can rewrite the statement around it is a password that can run anything."""
    found = [token for token in UNSAFE if token in value]
    if found:
        raise SystemExit(f"{name} may not contain: {' '.join(found)}")
    return value


def run_migrations(host: str, port: int, username: str, password: str, **passwords) -> list[str]:
    """Apply every file, substituting the `${...}` passwords. Connects as whoever it is
    given: creating users and tables needs more than datum-api ever holds."""
    passwords = {name: check_password(name, value) for name, value in passwords.items()}
    client = clickhouse_connect.get_client(
        host=host, port=port, username=username, password=password
    )
    applied = []
    for path in get_migrations():
        sql = Template(path.read_text()).substitute(**passwords)
        for statement in get_statements(sql):
            client.command(statement)
        applied.append(path.name)
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(prog="datum-migrate")
    parser.add_argument("--default-user-password", default="")
    parser.add_argument("--insights-user-password", required=True)
    arguments = parser.parse_args()

    host = os.environ.get("DATUM_CLICKHOUSE_HOST")
    datum_user_password = os.environ.get("DATUM_USER_PASSWORD")

    if not host:
        raise SystemExit("DATUM_CLICKHOUSE_HOST is unset. Source the env file first.")

    if not datum_user_password:
        raise SystemExit("DATUM_USER_PASSWORD is unset. Source the env file first.")

    applied = run_migrations(
        host=host,
        port=int(os.environ.get("DATUM_CLICKHOUSE_PORT", "8123")),
        username="default",
        password=arguments.default_user_password,
        DATUM_PASSWORD=datum_user_password,
        INSIGHTS_PASSWORD=arguments.insights_user_password,
    )
    for name in applied:
        print(f"applied {name}")


if __name__ == "__main__":
    main()
