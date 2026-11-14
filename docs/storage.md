# Storage

All data is in the `datum` database of the region's ClickHouse. The migrations in `datum/migrations/` make the database, the tables, the views and the users. Datum makes no schema. The migration files are the source of truth for each column.

## Tables

| Table | One row is | Sorted by | Kept for | Made by |
| --- | --- | --- | --- | --- |
| `samples` | One metric value | `resource_id, metric, ts` | `system_*` metrics 15 days, other metrics 90 days | `001_init_schema.sql` |
| `samples_recent` | A copy of a sample | `metric, ts, resource_id` | 2 days | `006_beacon.sql` |
| `samples_5m` | 5 minutes of one `system_*` series | `metric, resource_id, bucket` | 90 days | `006_beacon.sql` |
| `logs` | One log line | `resource_id, product, service, ts` | 30 days | `003_logs.sql` |
| `traces` | One span. A trace is the spans with one `trace_id`. | `resource_id, service, ts` | 7 days | `005_traces.sql` |
| `daily_ingestion_stats` | Samples of one resource on one day | `date, resource_id` | No limit | `002_ingestion_stats.sql` |
| `daily_log_stats` | Log lines of one resource, product, service and level on one day | `date, resource_id, product, service, level` | No limit | `004_log_stats.sql` |

Datum writes only `samples`, `logs` and `traces`. Materialized views fill the other tables from new rows. A materialized view sees only the rows that arrive after it exists. The one exception: the first run of `006_beacon.sql` copies the last 2 days of `samples` into `samples_recent`.

`resource_id` is a column, not a label, because it says who sent the row and it is the first sort key. Other labels stay in a map, so a sender can add a label with no migration. A label value adds one series for each value, so a label with a value for each site, request or job is not permitted.

`traces` keeps nanoseconds, because a span can be shorter than 1 millisecond. It has one partition for each day, so ClickHouse drops a full day when the TTL ends. A bloom filter on `trace_id` makes a read of one trace fast.

`daily_ingestion_stats` and `daily_log_stats` are `SummingMergeTree` tables. ClickHouse adds rows together only when it merges parts, so always read them with `sum()` and `GROUP BY`:

```sql
SELECT date, resource_id, sum(metric_count)
FROM datum.daily_ingestion_stats
WHERE date >= today() - 7
GROUP BY date, resource_id
ORDER BY 3 DESC;
```

## Views for Beacon

Alert rules read one metric across the region each minute. `samples` is sorted by resource first, so that read scans every resource. `samples_recent` keeps the last 2 days sorted by metric and time for these reads.

| View | Returns |
| --- | --- |
| `latest_value(metric, window)` | The newest value of each series of one metric in the last `window` seconds |
| `counter_increase(metric, window)` | The growth of each counter series in the last `window` seconds. A drop is a reset. |
| `system_history` | The `system_*` metrics with a `peak` column: full resolution for 14 days, then one row for each 5 minutes |

`latest_value` and `counter_increase` leave out samples that are more than 1 minute in the future, so a clock that runs fast does not keep the newest value. In `system_history`, the value of a counter (`_total`) is its last sample in the 5 minutes, so its growth stays correct. The value of a gauge is the average, and `peak` is the highest sample. Read `system_history` only with a filter on `metric` and `ts`. A rule reads `samples_recent`.

## Users

| ClickHouse user | Used by | Access |
| --- | --- | --- |
| `datum` | The Datum service | `SELECT` and `INSERT` on `datum.*` |
| `insights` | Insights | `SELECT`, `CREATE VIEW` and `DROP VIEW` on `datum.*` |
| `beacon` | Beacon | `SELECT` on `datum.*`, with the `beacon_reader` profile |

The `beacon_reader` profile fixes `readonly = 2`, a query limit of 20 seconds, 4 GB of memory, 4 threads and 100,000 result rows, so one slow alert rule cannot stop the server. `readonly = 2` lets Beacon set its output format and query parameters.

## Read data

Datum has no read route. A reader connects to ClickHouse with its own user:

```bash
clickhouse-client --user insights --query "
  SELECT ts, value FROM datum.samples
  WHERE resource_id = 'vm-abc123' AND metric = 'system_load_average'
  ORDER BY ts DESC LIMIT 10"
```

The grants of a user decide what it can read. To limit a reader to some resources, give its user a row policy:

```sql
CREATE ROW POLICY one_vm ON datum.samples USING resource_id = 'vm-abc123' TO some_reader;
```

Datum does not make row policies, because Datum does not know the readers.
