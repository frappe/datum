-- Log ingestion stats, per product, service and level.
CREATE TABLE IF NOT EXISTS datum.daily_log_stats
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

-- Materialized View
CREATE MATERIALIZED VIEW IF NOT EXISTS datum.mv_daily_log_stats
TO datum.daily_log_stats AS
SELECT
    toDate(ts) AS date,
    resource_id,
    product,
    service,
    level,
    count() AS log_count
FROM datum.logs
GROUP BY date, resource_id, product, service, level;