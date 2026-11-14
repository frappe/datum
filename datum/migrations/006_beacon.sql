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

-- The user Beacon reads with. readonly = 2 lets it set its output format and query parameters, and CONST keeps
-- the limits, so one slow rule cannot take the server.
CREATE SETTINGS PROFILE IF NOT EXISTS beacon_reader SETTINGS
    readonly = 2 CONST, max_execution_time = 20 CONST, max_memory_usage = 4000000000 CONST,
    max_threads = 4 CONST, max_result_rows = 100000 CONST, result_overflow_mode = 'throw' CONST;
CREATE USER IF NOT EXISTS beacon IDENTIFIED BY '${BEACON_PASSWORD}' SETTINGS PROFILE beacon_reader;
REVOKE ALL ON *.* FROM beacon;
GRANT SELECT ON datum.* TO beacon;
