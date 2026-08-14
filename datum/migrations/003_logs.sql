-- Log Lines Table. Sits under the same tenant boundary as samples; the token
-- stamps every line with resource_id.
CREATE TABLE IF NOT EXISTS datum.logs
(
    ts          DateTime64(3, 'UTC') CODEC(Delta, ZSTD),
    resource_id String CODEC(ZSTD),
    product     LowCardinality(String),
    service     LowCardinality(String),
    level       LowCardinality(String),
    source      LowCardinality(String),
    message     String CODEC(ZSTD),
    attributes  Map(LowCardinality(String), String)
)
ENGINE = MergeTree
PARTITION BY (toYear(ts), toQuarter(ts))
ORDER BY (resource_id, product, service, ts);