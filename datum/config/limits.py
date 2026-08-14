from __future__ import annotations

# Bytes after snappy, remote write only. Snappy runs to roughly 20:1 on
# protobuf, so the body size nginx allows does not bound what one costs open.
MAX_DECOMPRESSED = 12 * 1024 * 1024

# Readings in one write, both paths. Remote write counts them off the wire
# before parsing; the JSON path reaches it after, where pydantic already has the
# list in memory.
MAX_BATCH = 10_000

# Labels on one series. Nothing in the wire format bounds them, and a series
# carrying a million costs a fraction of a megabyte to send. Real producers use
# a handful: node_exporter's widest is well under twenty.
MAX_LABELS = 64

# Characters in a resource_id. Not a metric name, so the NAME rule does not apply.
MAX_RESOURCE_ID = 200

# Requests one tenant may make to one path per period, counted in memory by a
# single worker.
MAX_REQUESTS = 300
RATE_PERIOD = 60.0

# Seconds to connect or execute. Applied by ClickHouse rather than here, and
# overridable per deployment.
TIMEOUT = 30.0

# Log lines in one write. Higher than the metric batch cap because a Fluent Bit
# flush packs many lines, but bounded so one oversized flush cannot stream
# indefinitely into ClickHouse.
MAX_LOG_BATCH = 10_000

# Keys in one log line's `attributes` map. Same reasoning as `MAX_LABELS`: the
# wire format does not bound it, real producers carry a handful.
MAX_LOG_ATTRIBUTES = 64

# Characters in one log message. Reached after the body is parsed, so it bounds
# what ClickHouse is asked to store, not what datum holds.
MAX_LOG_MESSAGE = 8 * 1024
