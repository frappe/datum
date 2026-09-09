# datum

Datum stores telemetry about your fleet. Producers send it in. You read it back
out of ClickHouse with SQL.

It started as numbers only, and that is still most of what it holds. Answering
"is the fleet healthy" needs metrics; answering "what happened on that machine"
needs the log line, and "why was that request slow" needs the trace. Those cost
more to store, so each has its own table, its own shape, and its own retention
rather than being forced into the metrics one.

ClickHouse holds everything. Datum is the write door in front of it:

```
producers (JSON)          ──┐
agents    (remote write)  ──┤
collectors (OTLP traces)  ──┴──> datum ──> ClickHouse
                                             ▲
Insights  (SQL)           ───────────────────┘  its own read-only account
```

Every write carries a JWT. **Datum serves no reads.** Readers connect to
ClickHouse directly with their own account. That is the point of storing data in
something that already speaks SQL: a query endpoint in front of it would be a
second door onto the same rows, with its own access rules to keep correct.

Nothing is queued or buffered. If ClickHouse is down, a write fails and the
producer moves on.

---

# The service

## Endpoints

Everything real lives under `/v1`. `/health` does not, so a future `/v2` cannot
break a health check.

| Method   | Path                          | What it does                             |
| -------- | ----------------------------- | ---------------------------------------- |
| `POST`   | `/v1/ingest`                  | JSON samples in, `{"accepted": n}` out   |
| `POST`   | `/v1/ingest/remote`           | Prometheus remote write, `204` out       |
| `POST`   | `/v1/logs/ingest`             | JSON log lines in, `{"accepted": n}` out |
| `POST`   | `/v1/traces`                  | OTLP/HTTP spans in, empty `200` out      |
| `POST`   | `/v1/resource/add`            | register a machine, or change its status |
| `PUT`    | `/v1/resource/{id}/status`    | change one machine's status              |
| `DELETE` | `/v1/resource/{id}`           | mark a machine terminated                |
| `GET`    | `/health`                     | liveness                                 |

Browse them at `/docs`. Raw schema at `/v1/openapi.json`.

The three resource routes need an admin token. The four ingest routes need a
write token. See [Tokens](#tokens).

## The tables

Six tables and two views, all created by the migrations. Datum writes to four
of them; ClickHouse maintains the stats tables by itself.

**`datum.samples`** holds every reading:

```sql
CREATE TABLE datum.samples
(
    ts          DateTime64(3, 'UTC') CODEC(Delta, ZSTD),
    metric      LowCardinality(String),
    resource_id String CODEC(ZSTD),
    labels      Map(LowCardinality(String), String),
    value       Float64 CODEC(Gorilla, ZSTD)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (resource_id, metric, ts)
```

**Samples do not expire.** A reading is a few bytes and the fleet's history is
the point of keeping them, so cardinality is a permanent cost here: a label that
churns is a bill that never stops. That is the trade, and it is why the client
refuses `pid` and friends.

`resource_id` is a real column rather than a key in `labels`, because it decides
who a row belongs to, and that has to be a sort key. Every other label stays
open-ended in the map, so a producer can add one without a migration.

**`datum.resources`** holds the roster of machines:

```sql
CREATE TABLE datum.resources
(
    resource_id String,
    status      Enum8('Active' = 1, 'Terminated' = 2, 'Pending' = 3),
    updated_at  DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (resource_id)
```

`ReplacingMergeTree` keeps only the newest row per `resource_id`, so writing a
status is both the insert and the update. `updated_at` is what decides which row
is newest, which is why it is millisecond precision — with whole seconds, two
changes in the same second would tie and ClickHouse would pick either one.

**`datum.logs`** holds log lines from the fleet, under the same JWT and the same
`resource_id` tenant boundary:

```sql
CREATE TABLE datum.logs
(
    ts          DateTime64(3, 'UTC') CODEC(Delta, ZSTD),
    resource_id LowCardinality(String),
    product     LowCardinality(String),
    service     LowCardinality(String),
    level       LowCardinality(String),
    source      LowCardinality(String),
    message     String CODEC(ZSTD),
    attributes  Map(LowCardinality(String), String)
)
ENGINE = MergeTree
PARTITION BY (toYear(ts), toQuarter(ts))
ORDER BY (resource_id, product, service, ts)
```

`product` and `service` are sort keys, not map entries, because a fleet's reads
name them. `attributes` carries everything product-specific that no fleet-wide
query filters on. The same row policy that scopes `datum.samples` scopes this
table too, so a leaked read token cannot enumerate another tenant's log lines.

**`datum.traces`** holds spans. One row is one span; a trace is the rows sharing
a `trace_id`:

```sql
CREATE TABLE datum.traces
(
    ts             DateTime64(9, 'UTC') CODEC(Delta, ZSTD),
    resource_id    String CODEC(ZSTD),
    service        LowCardinality(String),
    span_name      LowCardinality(String),
    span_kind      LowCardinality(String),
    trace_id       String CODEC(ZSTD),
    span_id        String CODEC(ZSTD),
    parent_span_id String CODEC(ZSTD),
    duration_ns    UInt64 CODEC(T64, ZSTD),
    status_code    LowCardinality(String),
    status_message String CODEC(ZSTD),
    attributes     Map(LowCardinality(String), String),
    INDEX idx_trace_id trace_id TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_duration duration_ns TYPE minmax GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (resource_id, service, ts)
TTL toDateTime(ts) + toIntervalDay(7)
SETTINGS ttl_only_drop_parts = 1
```

Three things differ from the other tables, all on purpose:

- **Nanoseconds, not milliseconds.** A span can be shorter than a millisecond,
  and rounding one to zero loses the thing being measured.
- **Daily partitions.** The TTL is short, and dropping a day is a file delete.
- **Seven days.** A span is per request, so volume scales with traffic rather
  than with fleet size. Trace detail stops being useful long before metrics do.

`trace_id` sorts last, so it prunes nothing — the bloom filter is what makes
"fetch this one trace" cheap. `attributes` merges the span's own with its
resource's, which is why a resource's are capped: they are copied onto every
span the resource holds.

**`datum.daily_ingestion_stats`** counts how many samples each machine sent per
day. Datum never writes to it — `datum.mv_daily_ingestion_stats` is a
materialized view that watches inserts into `samples` and fills it in:

```sql
CREATE TABLE datum.daily_ingestion_stats
(
    date         Date,
    resource_id  String,
    metric_count SimpleAggregateFunction(sum, UInt64)
)
ENGINE = SummingMergeTree()
ORDER BY (date, resource_id);

CREATE MATERIALIZED VIEW datum.mv_daily_ingestion_stats
TO datum.daily_ingestion_stats AS
SELECT toDate(ts) AS date, resource_id, count() AS metric_count
FROM datum.samples
GROUP BY date, resource_id;
```

It exists to spot a machine that starts sending far more than it used to. A
`SummingMergeTree` only adds rows up when it merges, so read it with
`sum(metric_count)` and a `GROUP BY` rather than trusting one row per day:

```sql
SELECT date, resource_id, sum(metric_count)
FROM datum.daily_ingestion_stats
WHERE date >= today() - 7
GROUP BY date, resource_id ORDER BY 3 DESC;
```

The view only sees inserts made after it exists. It does not backfill from rows
already in `samples`.

**`datum.daily_log_stats`** is the log twin of the table above: how many lines
each machine sent per product, service and level, per day. `datum` never writes
to it — `datum.mv_daily_log_stats` is a materialized view that watches inserts
into `logs` and fills it in:

```sql
CREATE TABLE datum.daily_log_stats
(
    date         Date,
    resource_id  String,
    product      LowCardinality(String),
    service      LowCardinality(String),
    level        LowCardinality(String),
    log_count    UInt64
)
ENGINE = SummingMergeTree()
ORDER BY (date, resource_id, product, service, level);

CREATE MATERIALIZED VIEW datum.mv_daily_log_stats
TO datum.daily_log_stats AS
SELECT toDate(ts) AS date, resource_id, product, service, level, count() AS log_count
FROM datum.logs
GROUP BY date, resource_id, product, service, level;
```

It exists to spot a machine whose log volume shifts — an `error` spike reads at
a glance, where `daily_ingestion_stats` can only hear the overall count. The
same rules apply as its metric twin: read with `sum(log_count)` and a `GROUP BY`
rather than one row per day, and it only counts lines inserted after the view
existed.

`datum.traces` is the only table that expires, after 7 days. Nothing else does.

The line is what a row costs against what it is worth later. A sample is a few
bytes and one per machine per tick, so a year of them is both affordable and the
reason you collected them. A span is one per *request*, so its volume follows
traffic rather than fleet size — the ClickHouse write-up measures 200 million
spans a day from a moderate load — and nobody debugs a request from last March.

`logs` sits between the two and has no TTL today. If its volume starts scaling
with traffic rather than with machines, it will need one.

## Setting it up

`./setup.sh` does all three steps below in one go — it runs the migrations and
then starts the service. Export the `DATUM_*` variables first, plus
`DATUM_INSIGHTS_PASSWORD` for the insights user, and set
`DATUM_CLICKHOUSE_USER=datum`:

```bash
set -a && source .dev/datum.env && set +a
DATUM_INSIGHTS_PASSWORD=pick-another-one ./setup.sh
```

By hand it is three steps, in this order. Datum itself issues no DDL, so nothing exists until
the migrations run, and the service refuses to start without them.

**1. Write an env file.** Nothing generates it; see
[Configuration](#configuration) for the full list.

```bash
mkdir -p .dev
cat > .dev/datum.env <<'ENV'
DATUM_CLICKHOUSE_HOST=127.0.0.1
DATUM_CLICKHOUSE_PORT=8123
DATUM_CLICKHOUSE_USER=datum
DATUM_CLICKHOUSE_PASSWORD=pick-something
DATUM_JWT_PUBLIC_KEY_FILE=.dev/central.pub
ENV
```

**2. Run the migrations.** This creates the database, every table, and both
ClickHouse users.

```bash
set -a && source .dev/datum.env && set +a
uv run datum-migrate --insights-user-password 'pick-another-one'
```

`set -a` matters. Plain `source` makes shell variables, which child processes do
not inherit. `set -a` exports them; `set +a` turns that back off.

**3. Start the service.**

```bash
uv run uvicorn datum.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

### About the migrations

They live in `datum/migrations/` as plain `.sql` files, run in filename order:

| File | What it does |
|---|---|
| `000_acl.sql` | creates the `datum` and `insights` users and grants them |
| `001_init_schema.sql` | creates the database, `samples` and `resources` |
| `002_ingestion_stats.sql` | `daily_ingestion_stats` and the view that fills it |
| `003_logs.sql` | the `logs` table |
| `004_log_stats.sql` | `daily_log_stats` and the view that fills it |
| `005_traces.sql` | the `traces` table |

Everything is `IF NOT EXISTS`, so running it twice changes nothing.

`datum-migrate` connects as `default`, because `datum` is the user it is about
to create. Pass `--default-user-password` if your `default` has one. Creating
users needs `access_management` on that account, which a stock ClickHouse does
not enable — add a `users.d/` drop-in and restart:

```xml
<clickhouse>
    <users><default><access_management>1</access_management></default></users>
</clickhouse>
```

Two passwords go in:

- **datum's** comes from `DATUM_CLICKHOUSE_PASSWORD` in the env file. The
  migration creates the user with it, and the service later connects with it, so
  one value covers both.
- **insights'** is the `--insights-user-password` flag. Datum never connects as
  that user, so it is stored nowhere and has to be supplied each run.

Neither may contain `'`, `\`, `;`, `--` or a newline. Those characters would
change the SQL around them rather than sit inside it, so they are refused up
front rather than escaped.

Re-running does **not** change an existing user's password. `CREATE USER IF NOT
EXISTS` leaves it alone, so rotating means dropping the user first.

## Sending data

```bash
curl -X POST http://localhost:8000/v1/ingest \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"samples": [
        {"metric": "system_cpu_percent",
         "value": 12.5,
         "ts": "2026-08-05T10:00:00Z",
         "labels": {"host": "a"}}]}'
```

Up to 10,000 samples per batch. Metric and label names must match
`^[a-zA-Z_][a-zA-Z0-9_]*$` — datum refuses a bad name rather than storing
something nobody can query. That rule also refuses Prometheus names containing
`:`, which recording rules produce.

Producers that have numbers in hand and no agent should use
[`datum_client`](datum_client/README.md), which builds the names, checks the
labels and sends the batch.

There is no `resource_id` field in the body. It is not overridden, it is not
accepted: the token is the only thing that says who a row belongs to. A
`resource_id` smuggled into `labels` is dropped.

### Prometheus remote write

Point vmagent, Prometheus or the OTel collector at `/v1/ingest/remote`:

```yaml
remote_write:
  - url: http://localhost:8000/v1/ingest/remote
    authorization:
      credentials: YOUR_JWT
```

Snappy-compressed protobuf, v1 only — the version everything sends by default.
A v2 request is refused with a 415 naming the reason rather than half-decoded,
because v2 packs its strings into a lookup table that a v1 reader would silently
mangle. `__name__` becomes the metric, the rest become labels, and the token
still decides `resource_id`.

Answers `204` with an empty body, which is what remote write clients expect —
they retry anything else.

The same 10,000 sample cap applies, counted across every series in the request.
Over it the answer is `413` and nothing is stored. An agent will retry the same
oversized batch, so lower its `max_samples_per_send` rather than waiting for it
to drain.

### Logs

Point a log shipper (Fluent Bit, Vector, the OTel collector) at `/v1/logs/ingest`
with the same `Authorization: Bearer <jwt>` header:

```bash
curl -X POST http://localhost:8000/v1/logs/ingest \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"lines": [
        {"ts": "2026-08-05T10:00:00Z",
         "product": "pilot",
         "service": "worker",
         "level": "info",
         "source": "worker_pool.log",
         "message": "job done",
         "attributes": {"queue": "default"}}]}'
```

Up to 10,000 lines per batch. `product`, `service`, `level` and `source` must
match `^[a-zA-Z0-9_./-]{1,200}$`; attribute names must match
`^[a-zA-Z_][a-zA-Z0-9_]*$`; one message may be up to 8 KiB. There is no
`resource_id` field — a smuggled one inside `attributes` is dropped, exactly
as on the metrics path.

### Traces

Point an OTel collector at `/v1/traces`. It speaks OTLP/HTTP protobuf, gzipped
or not, and answers an empty `200` — which is what a collector reads as
delivered:

```yaml
exporters:
  otlphttp:
    endpoint: http://localhost:8000/v1/traces
    headers:
      Authorization: "Bearer ${DATUM_TOKEN}"

processors:
  batch:
    send_batch_size: 5000       # must stay under MAX_SPANS

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp]
```

vLLM and anything else OTel-instrumented points at the collector, not at datum.

The caps that matter to a collector's config:

| Cap | Value | What it bounds |
| --- | ----- | -------------- |
| `MAX_SPANS` | 10,000 | spans in one request |
| `MAX_SPAN_ATTRIBUTES` | 64 | attributes on one span, and on a row once its resource's are merged in |
| `MAX_ATTRIBUTE` | 8 KiB | one attribute, key and value together |
| `MAX_RESOURCE_ATTRIBUTES` | 8 KiB | all of one resource's attributes together |
| `MAX_DECOMPRESSED` | 12 MiB | what a gzipped body may become |

`MAX_RESOURCE_ATTRIBUTES` is the odd one and worth knowing about: a resource's
attributes are copied onto **every** span it holds, so 8 KiB of them across
10,000 spans is 78 MiB written from one request. That is why they get a tighter
budget than a span's own.

**A truncated upload is refused, not accepted.** Datum checks the gzip stream
actually finished. Answering `200` to a partial body would tell the collector
the spans were delivered, and it would never send them again.

`resource_id` comes from the token, as everywhere else. A `resource_id` in the
resource attributes is stored as an ordinary attribute; it does not decide
which machine the row belongs to.

## Managing resources

The roster of machines. These routes need a token with `"admin": true`.

```bash
curl -X POST http://localhost:8000/v1/resource/add \
  -H "Authorization: Bearer $ADMIN_JWT" -H "Content-Type: application/json" \
  -d '{"resource_id": "vm-abc123", "status": "Active"}'

curl -X PUT http://localhost:8000/v1/resource/vm-abc123/status \
  -H "Authorization: Bearer $ADMIN_JWT" -H "Content-Type: application/json" \
  -d '{"status": "Pending"}'

curl -X DELETE http://localhost:8000/v1/resource/vm-abc123 \
  -H "Authorization: Bearer $ADMIN_JWT"
```

Status is one of `Active`, `Terminated` or `Pending`. Anything else is a 422.

`DELETE` does not remove the row. It sets the status to `Terminated`, because
the machine's samples outlive it and datum holds no `ALTER` grant to delete
with.

This is the one place a `resource_id` comes from the request rather than the
token. An admin speaks for the whole fleet, so it has to name the machine it
means. Ingest is the opposite: there the token decides, always.

## Reading data

Not through datum. Point your reader at ClickHouse with its own account:

```bash
clickhouse-client --user insights --query \
  "SELECT ts, value FROM datum.samples
   WHERE resource_id = 'vm-abc123' AND metric = 'cpu' ORDER BY ts DESC LIMIT 10"
```

Datum used to carry a `/v1/query` passthrough plus two listing routes. All are
gone. They were the only place a caller's SQL reached the store, and keeping
them safe took four separate mechanisms — a row policy, a `readonly=1` setting,
a row cap and a startup grant audit — to guard a door Insights never used.

**Who may read what is decided by their ClickHouse account**, when it is
created. A reader that must not see the whole fleet gets a row policy of its
own:

```sql
CREATE ROW POLICY tenant ON datum.samples USING resource_id = 'vm-abc123' TO some_reader;
```

Datum does not create that policy, because datum does not know who reads.

### Capping request bodies

Datum does not cap the raw body; the reverse proxy does. Every limit in
`config/limits.py` is reached *after* the body is read, so without a proxy limit
a large POST is already in memory before anything checks it. The datum vhost
sets:

```nginx
client_max_body_size 32m;
```

Keep that line. A deployment that drops it has no bound on a request body at
all, and datum listens on `127.0.0.1:8000`, so anything reaching the port
directly is likewise uncapped.

The two protobuf paths are the exception: both bound what a compressed body may
*become*, because that is not the same as what arrives. Remote write reads
snappy's declared length before allocating against it; gzip declares none, so
the traces path bounds the decompression itself and refuses at one byte past
the cap rather than after materialising it.

## Tokens

Every call carries a JWT that Central signed: `Authorization: Bearer <jwt>`.

A machine's token:

```json
{
  "resource_id": "vm-abc123",
  "access": ["write"]
}
```

An admin's token:

```json
{
  "admin": true
}
```

**`resource_id` says which machine the numbers came from.** It is the only label
the token carries: one machine, one id. Everything else — which team owns it,
which cluster it sits in — is Central's to answer, not a label repeated on every
sample. Every row is stamped with it, so a token without one is a 401 rather
than a caller whose permissions are then judged.

**`access` says what the token may do.** Central signs bench logins, site logins
and enrolment tokens with the same key; without this, any of them would be
accepted here. Only `write` means anything now. A token carrying `read` keeps
the claim and is not refused for it, but there is nothing here to read.

**`admin: true` is what the resource routes need.** It must be the JSON boolean,
not the string `"true"` — a string would make any non-empty value an admin. An
admin speaks for the fleet rather than one machine, so it may carry no
`resource_id` at all. If it carries none it also cannot ingest, since there
would be nothing to stamp on the rows.

Signatures must be RSA or ECDSA. HMAC is never accepted.

## What each status means

| Status | Meaning |
|---|---|
| 200 | stored |
| 204 | stored, via remote write |
| 400 | ClickHouse refused the write, or the remote write body was unreadable |
| 401 | JWT missing, unsigned, expired, signed by the wrong key, or naming no `resource_id` when it is not an admin |
| 403 | the token may not do that: no `write` for ingest, not an admin for resources |
| 413 | decompressing past 12 MB, more than 10,000 samples or series, or a series wider than 64 labels |
| 415 | remote write v2, which is not read here |
| 422 | the request body broke the schema |
| 429 | too many requests for that route; `Retry-After` says how long |
| 503 | datum is up, ClickHouse is not |

## Configuration

Read by both `datum-migrate` and the service.

| Variable | Default | What it does |
|---|---|---|
| `DATUM_CLICKHOUSE_HOST` | required | where ClickHouse is |
| `DATUM_CLICKHOUSE_PORT` | `8123` | its HTTP port |
| `DATUM_CLICKHOUSE_USER` | `default` | who the service connects as; should be `datum` |
| `DATUM_CLICKHOUSE_PASSWORD` | empty | its password, and what the migration creates that user with |
| `DATUM_TIMEOUT` | `30` | seconds, connect and execute |
| `DATUM_JWT_PUBLIC_KEY_FILE` | none | the PEM file to check tokens against |
| `DATUM_OIDC_ISSUER` | none | fetch keys from an issuer instead. With neither, every call is a 401 |

With both key settings given, the issuer wins: keys come from its JWKS and the
token's `iss` must match. The PEM is used only when no issuer is set.

The database is always `datum` and the tables are always `samples`, `resources`
and `logs`. They are not configurable: the migrations name them too, and two
sources of truth would drift.

## Running it locally

```bash
docker run -d --name clickhouse -p 8123:8123 clickhouse/clickhouse-server

mkdir -p .dev
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out .dev/central.key
openssl rsa -in .dev/central.key -pubout -out .dev/central.pub
chmod 600 .dev/central.key

uv sync --all-groups
# then the three steps under "Setting it up"
```

`.dev/` is gitignored, so the test key cannot be committed by accident.

Then mint a token and use it:

```bash
JWT=$(uv run --with 'pyjwt[crypto]' python -c "
import jwt, time
from pathlib import Path
print(jwt.encode({'exp': int(time.time())+3600,
  'resource_id': 'vm-abc123', 'access': ['write']},
  Path('.dev/central.key').read_text(), algorithm='RS256'))")
```

Add `'admin': True` to those claims for a token that may manage resources.

Tokens last an hour. An expired one gives 401 and looks exactly like a wrong
key, so make a fresh one before hunting for a bug.

There is no installer. datum-api is one process; run it under whatever already
supervises your services, and install ClickHouse the way its own docs say.

## Working on it

```bash
uv run pytest
uv run ruff check .
```

The remote write message is generated. Change `remote_write.proto` and run:

```bash
uv run --with grpcio-tools python -m grpc_tools.protoc \
  -Idatum/api/internals/remote --python_out=datum/api/internals/remote \
  datum/api/internals/remote/remote_write.proto
```
