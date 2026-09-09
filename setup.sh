#!/usr/bin/env bash
# Migrates ClickHouse and starts datum-api. Needs the DATUM_* variables exported.
set -euo pipefail

: "${DATUM_CLICKHOUSE_HOST:?set it to where ClickHouse is}"
: "${DATUM_CLICKHOUSE_PASSWORD:?set it to the password for the datum user}"
: "${DATUM_INSIGHTS_PASSWORD:?set it to the password for the insights user}"

if [ -z "${DATUM_JWT_PUBLIC_KEY_FILE:-}" ] && [ -z "${DATUM_OIDC_ISSUER:-}" ]; then
    echo "set DATUM_JWT_PUBLIC_KEY_FILE or DATUM_OIDC_ISSUER, or every call is a 401" >&2
    exit 1
fi

cd "$(dirname "$0")"

uv sync --group api
uv run datum-migrate \
    --insights-user-password "$DATUM_INSIGHTS_PASSWORD" \
    --default-user-password "${DATUM_DEFAULT_PASSWORD:-}"

exec uv run uvicorn datum.api.app:create_app --factory \
    --host "${DATUM_HOST:-127.0.0.1}" --port "${DATUM_PORT:-8000}"
