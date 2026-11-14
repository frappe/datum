-- Machine metrics (system_*) are needed at full resolution only for a recent investigation. Datum keeps them
-- 15 days in samples, and 90 days as 5-minute values in samples_5m. All other metrics stay 90 days in samples.
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

ALTER TABLE datum.samples MODIFY TTL
    toDateTime(ts) + INTERVAL 15 DAY DELETE WHERE startsWith(metric, 'system_'),
    toDateTime(ts) + INTERVAL 90 DAY;

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
