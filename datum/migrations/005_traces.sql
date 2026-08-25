-- Traces Table. One row is one span; a trace is the rows sharing a trace_id.
-- Same tenant boundary as samples and logs: the token stamps every span.
CREATE TABLE IF NOT EXISTS datum.traces
(
    -- Nanoseconds, not milliseconds: a span can be shorter than one.
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
    -- trace_id sorts last, so fetching one trace needs this.
    INDEX idx_trace_id trace_id TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_duration duration_ns TYPE minmax GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (resource_id, service, ts)
TTL toDateTime(ts) + toIntervalDay(7)
SETTINGS ttl_only_drop_parts = 1;
