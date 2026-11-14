# API

Datum takes four kinds of data. Each write route is under `/v1` and needs a token with `write` access. See [Tokens](tokens.md). `/health` is not under `/v1`, so a later `/v2` does not change the health check.

| Method | Path | Takes | Answers |
| --- | --- | --- | --- |
| `POST` | `/v1/ingest` | JSON samples | `{"accepted": n}` |
| `POST` | `/v1/ingest/remote` | Prometheus remote write v1 | `204` |
| `POST` | `/v1/logs/ingest` | JSON log lines | `{"accepted": n}` |
| `POST` | `/v1/traces` | OTLP/HTTP protobuf spans | An empty `200` |
| `GET` | `/health` | Nothing | `200` when ClickHouse answers, else `503` |

The OpenAPI page is at `/docs`, and the schema is at `/v1/openapi.json`.

The token sets the `resource_id` of each row. The body has no `resource_id` field. Datum drops a `resource_id` in the labels or the attributes of samples and log lines. In a trace, a `resource_id` resource attribute stays an ordinary attribute.

## Metrics

```bash
curl -X POST https://telemetry-svc.example.com/v1/ingest \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"samples": [{"metric": "system_load_average", "value": 0.42, "ts": "2026-08-05T10:00:00Z", "labels": {"window": "5m"}}]}'
```

A metric name and a label name must match `^[a-zA-Z_][a-zA-Z0-9_]*$`. Datum refuses a bad name, because nobody can query it. This also refuses a Prometheus name with `:`, which recording rules make. A sender in Python uses [`datum_client`](../datum_client/README.md), which builds valid names and sends the batch.

### Prometheus remote write

Point vmagent, Prometheus or an OpenTelemetry collector at `/v1/ingest/remote`:

```yaml
remote_write:
  - url: https://telemetry-svc.example.com/v1/ingest/remote
    authorization:
      credentials: YOUR_JWT
```

Datum reads snappy-compressed protobuf of version 1, which every client sends by default. It refuses version 2 with `415`, because version 2 keeps its strings in a lookup table that a version 1 reader reads wrongly. `__name__` becomes the metric, and the other labels stay labels.

Datum answers `204` with no body, because a remote write client sends again on any other answer. The 10,000 sample limit counts all series of one request. Over it, Datum answers `413` and stores nothing. A client sends the same batch again, so lower its `max_samples_per_send`.

## Log lines

```bash
curl -X POST https://telemetry-svc.example.com/v1/logs/ingest \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"lines": [{"ts": "2026-08-05T10:00:00Z", "product": "pilot", "service": "worker", "level": "error", "source": "journal", "message": "job failed", "attributes": {"queue": "default"}}]}'
```

The body can also be a bare array of lines. The Datum agent sends that form.

| Field | Rule |
| --- | --- |
| `product`, `service`, `source` | Match `^[a-zA-Z0-9_./-]{1,200}$` |
| `level` | One of `emergency`, `alert`, `critical`, `error`, `warning`, `notice`, `info`, `debug` |
| `message` | 8 KiB or less |
| `attributes` | 64 names or fewer. Each name matches `^[a-zA-Z_][a-zA-Z0-9_]*$` |

## Traces

Point an OpenTelemetry collector at `/v1/traces`. Datum reads OTLP/HTTP protobuf, with or without gzip, and answers an empty `200`. A program that has OpenTelemetry sends to the collector, not to Datum.

```yaml
exporters:
  otlphttp:
    endpoint: https://telemetry-svc.example.com/v1/traces
    headers:
      Authorization: "Bearer ${DATUM_TOKEN}"

processors:
  batch:
    send_batch_size: 5000 # less than 10,000 spans

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp]
```

Datum refuses a gzip body that does not end correctly. A `200` for a part of a body tells the collector that all spans arrived, and the collector does not send them again.

The attributes of a resource go on each span of that resource. So 8 KiB of resource attributes on 10,000 spans writes 78 MiB from one request. That is why the limit for resource attributes is smaller than the limit for span attributes.

## Limits

| Limit | Value |
| --- | --- |
| Requests to one write route | 12 in 60 seconds for each resource. Each worker counts on its own. |
| Samples, or remote write samples of all series | 10,000 in one request |
| Labels on one sample or one series | 64 |
| Log lines | 10,000 in one request |
| Spans | 10,000 in one request |
| Attributes on one span, with the resource attributes | 64 |
| One span attribute, name and value | 8 KiB |
| All attributes of one resource | 8 KiB |
| A decompressed remote write or trace body | 12 MiB |

Datum applies these limits after it reads the body. Caddy on the Datum host sets no body limit, because a sender sends all it kept during an outage in one batch. Only the two protobuf routes limit what a body becomes after decompression. Remote write reads the length that snappy declares before it makes room for the body. Gzip declares no length, so the traces route stops at 1 byte past the limit while it decompresses.

## Status codes

| Status | Meaning |
| --- | --- |
| 200 | Stored |
| 204 | Stored, from remote write |
| 400 | ClickHouse refused the write, or Datum cannot read the remote write body |
| 401 | The token is missing, expired or not valid, or it has no `resource_id` |
| 403 | The token has no `write` access |
| 413 | A body over 12 MiB after decompression, too many samples, series or spans, or a series with more than 64 labels |
| 415 | Remote write version 2 |
| 422 | The body does not match the schema |
| 429 | Too many requests to the route. `Retry-After` gives the seconds to wait. |
| 503 | Datum runs, but ClickHouse does not answer |

For each refused `/v1` request, Datum writes one JSON line to stderr with `route`, `status` and `sender`. `sender` is the `resource_id` of the token when the token is valid. A 4xx line has level `warning`, and a 5xx line has level `error`. The Datum agent on the Datum host sends these lines to `logs`, so a rule can find the sender that loses data.
