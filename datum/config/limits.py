from __future__ import annotations

# Bytes a compressed body may become, on either protobuf path. Snappy runs to
# roughly 20:1, so the body size nginx allows does not bound what one costs
# open. Remote write checks it against snappy's declared length; gzip declares
# none, so traces check what came out.
MAX_DECOMPRESSED = 12 * 1024 * 1024

# Readings in one write, both paths. Remote write counts them off the wire
# before parsing; the JSON path reaches it after, where pydantic already has the
# list in memory.
MAX_BATCH = 10_000

# Labels on one series. Nothing in the wire format bounds them, and a series
# carrying a million costs a fraction of a megabyte to send. Real producers use
# a handful: node_exporter's widest is well under twenty.
MAX_LABELS = 64

# Spans in one OTLP request, counted off the wire before protobuf builds them.
# A collector's send_batch_size must not exceed this or its exports are refused.
MAX_SPANS = 10_000

# Attributes on one span. OTel bounds neither these nor a body's span count.
MAX_SPAN_ATTRIBUTES = 64

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
