# Ship logs with Vector

Use Vector 0.59.0. The app supplies sources and a remap named `*_to_datum`; this package supplies normalization, batching, authentication and the HTTP sink. Datum needs its existing `POST /v1/logs/ingest` endpoint, with no server changes.

```python
from datum_client.vector import install

install("/etc/my-app/vector", endpoint="https://datum.example.com", token=token)
```

`install(directory, *, endpoint, token)` returns the installed `datum.yaml` path. It writes that file and `datum/endpoint` and `datum/token` atomically with mode 0600. The secret directory has mode 0700. The endpoint must be an HTTP(S) base URL, without a path, query, fragment or embedded credentials. Invalid arguments leave the installation untouched. Repeating the call replaces the files; replacements are atomic per file, not a transaction across all files. Serialize installation and reload in the application.

For config editing, rootless binary installation and user/system systemd services, use
the separate [datum_exporter library and CLI](../../datum_exporter/README.md).

To configure Vector manually, write `app.yaml` next to the installed config:

```yaml
data_dir: /var/lib/my-app/vector
sources:
  app:
    type: file
    include: [/var/log/my-app/*.log]
    read_from: end
transforms:
  app_to_datum:
    type: remap
    inputs: [app]
    source: '.product = "my-app"'
```

Create the writable data directory and run as the user who owns the logs and secrets:

```sh
cd /etc/my-app/vector
vector --config app.yaml --config datum.yaml
```

The working directory matters: the directory secret backend in `datum.yaml` reads `./datum/endpoint` and `./datum/token`. Config paths alone do not set it. Reserve the component names `datum_line`, `datum`, and the `*_to_datum` suffix for this pipeline. The secret backend belongs in the shared config, not the app config.

## Record contract

| Datum field | Input and fallback |
| --- | --- |
| `message` | First nonempty `message`, `msg`, `MESSAGE`, or `log`; scalar conversion, otherwise empty; limited to 8192 characters. |
| `level` | `level`, `severity`, `lvl`, or syslog `PRIORITY`; lowercased; invalid values become `info`. |
| `product` | `product`, or `unknown`. |
| `service` | `service`, `SYSLOG_IDENTIFIER`, `_SYSTEMD_UNIT`, or `unknown`. |
| `source` | `source`, basename of `file`, `_SYSTEMD_UNIT`, or `unknown`. |
| `ts` | Vector's `timestamp`, parsed when needed; missing or invalid values use processing time. |
| `attributes` | Up to 64 remaining scalar fields converted to strings. Empty keys and nested objects/arrays are skipped. |

Names are sanitized to Datum's patterns. Product, service and source retain slashes; explicit source names retain directories. Sanitized attribute-name collisions keep one value. Source metadata `host` and `source_type`, and an input `ts`, are discarded. Parse application timestamps into `timestamp` in the app remap.

## Delivery and limits

The sink wraps its JSON array as `{"lines": [...]}`. It batches at most 10,000 events, targets 256 KiB before serialization, and flushes after five seconds. One concurrent request and a limit of 12 requests per 60 seconds match Datum's log route. Memory buffering and acknowledgements let file sources replay unaccepted records after restart, while the source files still exist. Keep files long enough for outage recovery. Delivery is at least once: a lost HTTP acknowledgement can cause duplicates.

The byte target is not a hard HTTP-body limit. JSON escaping and an individual large event can exceed it. Verify actual payload sizes against your reverse proxy, especially for large attributes, which Datum does not bound by bytes. A `413`, `401` or `422` is terminal under Vector's default retry policy and drops the batch. Transient failures are retried; prolonged outages apply backpressure.

The helper does not mint or renew tokens. Before expiry, replace credentials and reload Vector so it rereads secrets. The token determines resource identity; the exporter does not create or route identities. HTTP sink health checks are disabled because the ingestion route does not support a GET probe.

## Tests

With Vector on `PATH`, run `uv run pytest tests/test_vector.py`. This runs the `vector test` specifications and posts real payloads to a local receiver that validates them with Datum's `LogLineRequest`. Without Vector, only the Python installation tests run. CI installs the pinned binary so the Vector tests cannot silently remain skipped.
