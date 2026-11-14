# Setup

Cargo installs Datum on the Datum host of each region, behind Caddy. Datum runs there as 2 uvicorn workers on `127.0.0.1:8000`. The `DATUM_WORKERS` variable of the Cargo installer changes the number. This page tells how to run Datum yourself.

Datum makes no schema, so it does not start until the migrations run. Do the steps in this order.

## 1. Configure

Datum and `datum-migrate` read the same variables.

| Variable | Default | What it does |
| --- | --- | --- |
| `DATUM_CLICKHOUSE_HOST` | Required | The ClickHouse host |
| `DATUM_CLICKHOUSE_PORT` | `8123` | The HTTP port of ClickHouse |
| `DATUM_CLICKHOUSE_USER` | `default` | The user of the service. Set it to `datum`. |
| `DATUM_USER_PASSWORD` | Required | The password of the `datum` user. The migrations make the user with it. |
| `DATUM_TIMEOUT` | `30` | Seconds to connect and to run a query |
| `DATUM_JWKS_URL` | None | The full URL of the key set. See [Tokens](tokens.md). |
| `DATUM_REGION_ID` | None | The region of this Datum. A token must name it in `aud`. |

Without `DATUM_JWKS_URL` or `DATUM_REGION_ID`, every write gets `401`. The database is always `datum`, because the migrations name it too.

```bash
mkdir -p .dev
cat > .dev/datum.env <<'ENV'
DATUM_CLICKHOUSE_HOST=127.0.0.1
DATUM_CLICKHOUSE_PORT=8123
DATUM_CLICKHOUSE_USER=datum
DATUM_USER_PASSWORD=pick-something
DATUM_JWKS_URL=https://cargo.example.com/api/method/cargo.api.jwks.get_jwks
DATUM_REGION_ID=1
ENV
```

## 2. Run the migrations

```bash
set -a && source .dev/datum.env && set +a
INSIGHTS_USER_PASSWORD='pick-another-one' BEACON_USER_PASSWORD='and-another' uv run datum-migrate
```

`set -a` exports the variables, so `datum-migrate` gets them.

`datum-migrate` runs the files in `datum/migrations/` in name order. [Storage](storage.md) tells what each table keeps.

| File | Makes |
| --- | --- |
| `000_acl.sql` | The `datum` and `insights` users and their grants |
| `001_init_schema.sql` | The database and `samples` |
| `002_ingestion_stats.sql` | `daily_ingestion_stats` and its materialized view |
| `003_logs.sql` | `logs` |
| `004_log_stats.sql` | `daily_log_stats` and its materialized view |
| `005_traces.sql` | `traces` |
| `006_beacon.sql` | `samples_recent`, `samples_5m`, the Beacon views, the `beacon` user and its profile, and the TTL of `samples` and `logs` |

Each statement uses `IF NOT EXISTS`, so a second run changes nothing.

`datum-migrate` connects as `default`, because it makes the `datum` user. Set `DEFAULT_USER_PASSWORD` when `default` has a password. To make users, `default` needs `access_management`. A new ClickHouse does not have it, so add a file in `users.d/` and restart ClickHouse:

```xml
<clickhouse>
    <users><default><access_management>1</access_management></default></users>
</clickhouse>
```

All passwords come from the environment, so none shows in the process list.

| Variable | User |
| --- | --- |
| `DATUM_USER_PASSWORD` | `datum`. The service uses the same value to connect. |
| `INSIGHTS_USER_PASSWORD` | `insights` |
| `BEACON_USER_PASSWORD` | `beacon` |
| `DEFAULT_USER_PASSWORD` | `default`. Optional. |

A password cannot contain `'`, `\`, `;`, `--` or a new line, because these change the SQL around the password. `datum-migrate` refuses them.

A second run does not change the password of a user that exists. To change a password, drop the user first.

## 3. Start the service

```bash
uv run uvicorn datum.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

`GET /health` returns `200` when ClickHouse answers.

## Run it locally

Start ClickHouse and install the dependencies:

```bash
docker run -d --name clickhouse -p 8123:8123 clickhouse/clickhouse-server
uv sync --all-groups
```

Make a test issuer. This key set has one Central key, so it needs no Atlas or Cargo:

```bash
mkdir -p .dev
uv run python -c "
import json
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm
key = Ed25519PrivateKey.generate()
Path('.dev/central.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
jwk = json.loads(OKPAlgorithm.to_jwk(key.public_key()))
Path('.dev/jwks.json').write_text(json.dumps({'keys': [jwk | {'kid': 'central:dev', 'alg': 'EdDSA', 'use': 'sig'}]}))"
python3 -m http.server 8799 --directory .dev &
```

Then do the three steps above with `DATUM_JWKS_URL=http://127.0.0.1:8799/jwks.json` and `DATUM_REGION_ID=1`. `.dev/` is in `.gitignore`, so git does not commit the test key.

Make a token for the region and send a sample:

```bash
JWT=$(uv run python -c "
import jwt, time
from pathlib import Path
now = int(time.time())
claims = {'iss': 'central', 'aud': 'atlas-datum:1', 'iat': now, 'exp': now + 3600, 'resource_id': 'vm-abc123', 'access': ['write']}
print(jwt.encode(claims, Path('.dev/central.key').read_text(), algorithm='EdDSA', headers={'kid': 'central:dev'}))")

curl -X POST http://127.0.0.1:8000/v1/ingest \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d "{\"samples\": [{\"metric\": \"system_load_average\", \"value\": 0.42, \"ts\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"labels\": {\"window\": \"5m\"}}]}"
```

The token lasts 1 hour. An expired token gets `401`, the same as a wrong key, so make a new token before you look for a bug.

## Change the remote write message

The Python code of the remote write message is generated. After a change to `remote_write.proto`, run:

```bash
uv run --with grpcio-tools python -m grpc_tools.protoc \
  -Idatum/api/internals/remote --python_out=datum/api/internals/remote \
  datum/api/internals/remote/remote_write.proto
```
