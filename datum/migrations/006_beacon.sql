-- Alert rules read the last minutes of many series every minute. samples is ordered by resource first, so a query for one
-- metric across the fleet would read every resource. samples_recent keeps 2 days ordered by metric and time, filled by a view.
CREATE TABLE IF NOT EXISTS datum.samples_recent
(
    ts          DateTime64(3, 'UTC') CODEC(Delta, ZSTD),
    metric      LowCardinality(String),
    resource_id String CODEC(ZSTD),
    labels      Map(LowCardinality(String), String),
    value       Float64 CODEC(Gorilla, ZSTD)
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (metric, ts, resource_id)
TTL toDateTime(ts) + INTERVAL 2 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS datum.samples_recent_mv TO datum.samples_recent AS
SELECT ts, metric, resource_id, labels, value FROM datum.samples;

-- The first run copies the last 2 days that the view did not see. Later runs copy nothing: every row after the
-- oldest one in samples_recent came through the view.
INSERT INTO datum.samples_recent
SELECT ts, metric, resource_id, labels, value FROM datum.samples
WHERE ts > now() - INTERVAL 2 DAY
  AND ts < (SELECT if(count() = 0, now64(3), min(ts)) FROM datum.samples_recent);

-- Machine metrics (system_*) need full resolution only for a recent investigation, so samples_5m keeps one row for each 5 minutes.
CREATE TABLE IF NOT EXISTS datum.samples_5m
(
    bucket      DateTime('UTC') CODEC(Delta, ZSTD),
    metric      LowCardinality(String),
    resource_id String CODEC(ZSTD),
    labels      Map(LowCardinality(String), String),
    last        AggregateFunction(argMax, Float64, DateTime64(3, 'UTC')),
    average     AggregateFunction(avg, Float64),
    peak        SimpleAggregateFunction(max, Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(bucket)
ORDER BY (resource_id, metric, labels, bucket)
TTL bucket + INTERVAL 90 DAY;

-- Each insert holds about one sample of a series, so the merges of samples_5m build the 5-minute values.
CREATE MATERIALIZED VIEW IF NOT EXISTS datum.samples_5m_mv TO datum.samples_5m AS
SELECT toStartOfFiveMinutes(ts) AS bucket, metric, resource_id, labels,
       argMaxState(value, ts) AS last, avgState(value) AS average, max(value) AS peak
FROM datum.samples
WHERE startsWith(metric, 'system_')
GROUP BY bucket, metric, resource_id, labels;

-- Machine metrics stay 15 days at full resolution and 90 days in samples_5m. Other metrics stay 90 days, log lines 30 days.
ALTER TABLE datum.samples MODIFY TTL
    toDateTime(ts) + INTERVAL 15 DAY DELETE WHERE startsWith(metric, 'system_'),
    toDateTime(ts) + INTERVAL 90 DAY;
ALTER TABLE datum.logs MODIFY TTL toDateTime(ts) + INTERVAL 30 DAY;

-- The newest value of each series of one metric in the last `window` seconds. A sample from a clock that runs fast must not
-- stay the newest value, so both views leave out samples more than 1 minute in the future.
CREATE OR REPLACE VIEW datum.latest_value AS
SELECT resource_id, labels, argMax(value, ts) AS value, max(ts) AS last_ts
FROM datum.samples_recent
WHERE metric = {metric:String} AND ts > now() - toIntervalSecond({window:UInt32}) AND ts <= now() + INTERVAL 1 MINUTE
GROUP BY resource_id, labels;

-- The growth of each counter series of one metric in the last `window` seconds. A drop is a reset, so the new value is growth.
CREATE OR REPLACE VIEW datum.counter_increase AS
SELECT resource_id, labels, length(vals) AS samples,
       arraySum(arrayMap((d, v) -> if(d >= 0, d, v), arrayDifference(vals), vals)) AS increase
FROM (
    SELECT resource_id, labels, arrayMap(x -> x.2, arraySort(groupArray((ts, value)))) AS vals
    FROM datum.samples_recent
    WHERE metric = {metric:String} AND ts > now() - toIntervalSecond({window:UInt32}) AND ts <= now() + INTERVAL 1 MINUTE
    GROUP BY resource_id, labels
);

-- The history of the system_* metrics in the columns of samples: full resolution for the last 14 days, and one row
-- for each 5 minutes before that. The value of a counter (_total) is its last sample, so its growth stays correct.
-- The value of a gauge is the average, and peak keeps a short peak. Read it only with a filter on metric and time.
CREATE OR REPLACE VIEW datum.system_history AS
SELECT ts, metric, resource_id, labels, value, value AS peak
FROM datum.samples
WHERE startsWith(metric, 'system_') AND ts > now() - INTERVAL 14 DAY
UNION ALL
SELECT toDateTime64(bucket, 3, 'UTC') AS ts, metric, resource_id, labels,
       if(endsWith(metric, '_total'), argMaxMerge(last), avgMerge(average)) AS value, max(peak) AS peak
FROM datum.samples_5m
WHERE bucket <= now() - INTERVAL 14 DAY
GROUP BY bucket, metric, resource_id, labels;

-- The user Beacon reads with. readonly = 2 lets it set its output format and query parameters, and CONST keeps
-- the limits, so one slow rule cannot take the server.
CREATE SETTINGS PROFILE IF NOT EXISTS beacon_reader SETTINGS
    readonly = 2 CONST, max_execution_time = 20 CONST, max_memory_usage = 4000000000 CONST,
    max_threads = 4 CONST, max_result_rows = 100000 CONST, result_overflow_mode = 'throw' CONST;
CREATE USER IF NOT EXISTS beacon IDENTIFIED BY '${BEACON_PASSWORD}' SETTINGS PROFILE beacon_reader;
REVOKE ALL ON *.* FROM beacon;
GRANT SELECT ON datum.* TO beacon;
