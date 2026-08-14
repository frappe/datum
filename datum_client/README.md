# datum_client

Send metrics to Datum. No HTTP code, no made-up metric names.

Standard library only. Nothing from the Datum service. No extra installs.

```python
from datum_client import Batch, Datum

datum = Datum("https://datum.internal:8000", token=DATUM_JWT)

memory = Batch("system", "memory")
memory.gauge("used", 1154545090, "bytes")

datum.record(memory)
```

That is the whole loop. `record` does not post — it buffers, and posts only
once the buffer is full or the interval has passed. Call it every tick.

## The three pieces

**`Datum`** — where to send, and the token. Make one, keep it.

**`Batch`** — a bag of samples that share a name prefix and some labels.

**`record`** — hands batches to the client. Posts only when one is due.

```
Batch("pilot", "process", bench="bench_0001")
  │       │         │            └─ label added to every sample here
  │       │         └─ subsystem
  │       └─ namespace
  └─ each gauge/counter/up/info call adds one sample

record(batch, other) -> buffer -> 1 POST per 500 samples or 10 seconds
```

## When it actually posts

There is no background thread. The drain runs on whichever `record` finds one
owed, so exactly one call in every interval pays for the request and the rest
are close to free.

| Setting | Default | Means |
|---|---|---|
| `flush_at` | 500 | samples buffered before the next `record` drains |
| `flush_interval` | 10.0 | seconds a sample may sit before the next `record` drains |
| `timeout` | 2.0 | seconds one POST may take |

**The client never throws a sample away.** There is no buffer ceiling and no
trimming: a sample leaves the buffer by being posted. Hand over more than
`flush_at` in one go and it ships in as many requests as it takes, chunked to
10 000 each, which is what datum accepts.

Which makes the buffer yours to size. A buffered sample costs about 550 bytes,
and the buffer holds at most `flush_at` plus one batch, so the default sits
around 5 MB and `flush_at=100_000` would be roughly 55 MB. Nothing here caps
it — a client that trims to protect its own memory is a client that loses your
numbers.

Two consequences worth knowing:

- **A process that stops recording holds its tail.** Nothing drains on its own.
  The tail goes out when the client is collected, and failing that at exit, so
  a short-lived client that falls out of scope still sends what it buffered.
  A `SIGKILL` loses it — same trade the service makes, a gap in a chart beats a
  stalled producer.
- **If your host has a scheduler, point it at `flush()`.** It is public and
  cheap when nothing is due, and calling it on a timer takes the POST off the
  producer's tick entirely. The interval then only matters as a backstop.

```python
datum.flush()  # post now, returns how many samples went
datum.close()  # flush and stop tracking this client
```

`Datum` is also a context manager, which flushes on the way out:

```python
with Datum(url, token=DATUM_JWT) as datum:
    datum.record(batch)
```

If datum is unreachable the samples are dropped rather than retried — but by
the network, not by the client. Nothing here ever raises because of it.

## Naming

You pass the parts. The client joins them:

`namespace_subsystem_target_unit_suffix`

```python
batch = Batch("pilot", "process", bench="bench_0001")

batch.gauge("cpu", 1.2, "percent", service="web")
# pilot_process_cpu_percent{bench="bench_0001",service="web"} 1.2

batch.counter("io_read", 4096, "bytes", service="web")
# pilot_process_io_read_bytes_total{...} 4096
```

The namespace and subsystem live on the batch. Call sites only pass what
changes. That is what keeps names the same everywhere.

| Method | Adds | Use for |
|---|---|---|
| `gauge` | nothing | goes up and down: a size, a percent, a queue depth |
| `counter` | `_total` | only goes up, so a reader knows to diff it |
| `up` | `_up` | 1 alive, 0 dead |
| `info` | `_info` | always 1, facts sit in the labels |

Leave `target` empty when the prefix already says it all:

```python
Batch("pilot", "process").up("", True, service="web")  # pilot_process_up
```

Every method returns the batch, so you can chain:

```python
Batch("system", "memory").gauge("used", u, "bytes").gauge("free", f, "bytes")
```

One batch has one subsystem. Need more? Make more batches and record them
together.

```python
memory = Batch("system", "memory")
memory.gauge("used", used_bytes, "bytes")  # system_memory_used_bytes

cpu = Batch("system", "cpu")
cpu.gauge("usage", 12.5, "percent")  # system_cpu_usage_percent

datum.record(memory, cpu)
```

Batches stay separate on purpose. One can never write into another.

## Units

The unit is glued onto the name. Same as `prometheus_client`.

```python
memory.gauge("used", 1154545090, "bytes")  # system_memory_used_bytes
```

**It is not checked.** Pass `"mb"` and you get `system_memory_used_mb`.

Use base units: `bytes`, `seconds`, `ratio`, `percent`. Do the maths yourself:

```python
memory.gauge("used", record["used_mb"] * 1024 * 1024, "bytes")
timing.gauge("request", elapsed_ms / 1000, "seconds")
```

Why bother? A name like `..._mb` puts a hidden ×1048576 into every query and
every alert after it. Once data is stored you cannot fix the name.

No unit is fine when a unit means nothing. A load average is not a quantity of
anything:

```python
batch.gauge("load_average", 0.42, window="1m")  # system_load_average
```

The unit is not added twice if the target already ends with it:

```python
batch.gauge("memory_rss", 1024, "bytes")  # pilot_process_memory_rss_bytes
batch.gauge("memory_rss_bytes", 1024, "bytes")  # pilot_process_memory_rss_bytes
```

`MB` and `MiB` are rejected. Not for being scaled — for having capitals. Names
must be lowercase.

## Labels that break the store

First, what a **series** is: the full set of labels, name included, treated as
one identity. Change any label value and it is a different series.

So one label costs you its number of different values:

```
500 samples, labels never change   ->   1 series
500 samples, pid changes each time -> 500 series
```

Same 500 numbers. The second one is 500 times more expensive.

When a process restarts, the old series is not updated. It is left behind,
holding a few old numbers. Nothing expires, so it is left behind for good.

These labels are rejected: `pid`, `container_id`, `request_id`, `trace_id`,
`uuid`.

```python
batch.gauge("cpu", 1.2, "percent", pid="4412")
# BadName: 'pid' changes constantly, so it would make a new series each time.
#          Label by service instead, and put it on an _info metric.

batch.info("", service="web", pid="4412")  # allowed here only
# pilot_process_info{service="web",pid="4412"} 1
```

**`_info` helps, it does not fix.** pid on 8 metrics means 8 new series per
restart. On `_info` only, 1. Still grows forever: 1000 hosts × 6 services ×
one restart a day is about 2.2 million series a year.

What you get is that the metrics you query stay at one series per service. Your
dashboards do not slow down. And the mess sits in one metric you can drop.

If nothing looks up pid, do not send it. A pid only matters while the process
is alive, and the host can tell you that.

This list is only names someone thought of. `worker_pid`, `job_id`, `session`
and `sha` get through. To see which label is growing, ask the store:

```sql
SELECT arrayJoin(mapKeys(labels)) AS label, uniq(labels[label]) AS values
FROM datum.samples WHERE metric = 'pilot_process_cpu_percent'
GROUP BY label ORDER BY values DESC
```

Label names must be lowercase with underscores. Labels on `Batch(...)` are
checked too, when you make it.

## Say when something is missing

A missing series shows nothing on a dashboard. A 0 can raise an alert.

```python
for service, pid in targets.items():
    alive = pid is not None
    batch.up("", alive, service=service)
    if not alive:
        continue
    batch.gauge("cpu", cpu_percent(pid), "percent", service=service)
```

## Text is not a number

Datum only stores numbers. Put the text in a label, set the value to 1:

```python
batch.gauge("state", 1, service="web", state="S")
# pilot_process_state{service="web",state="S"} 1
```

Then `pilot_process_state{state="Z"} == 1` finds zombies.

## Timestamps

Every sample needs one. Datum will not add it for you. A server clock says when
the data arrived, not when it was measured. Those differ when things are slow.

The client uses `datetime.now(UTC)`. That is producer time, so it is right.

Pass `ts=` when you took the reading earlier:

```python
taken = datetime.fromisoformat(record["time"])
batch.gauge("cpu", record["cpu_percent"], "percent", ts=taken)
```

Use the same `ts` for every sample from one reading, so they line up.

Stored in milliseconds. Anything finer is lost.

## Sending

```python
datum.record(batch)
datum.record(system, memory, cpu)  # buffered, posted when due
```

Fire and forget. One POST, 2 second timeout, no retry, no queue on disk.
A lost metric is a gap in a chart. Blocking the producer to retry is worse.

If you already run vmagent, Prometheus or an OTel collector, you do not need
this client at all — point its `remote_write` at `/v1/ingest/remote` instead.
Same token, same rules. This client exists for producers that have numbers in
hand and no agent to hand them to.

- Network failure does **not** raise. Nothing `record` or `flush` does raises
  because datum was unreachable.
- `401` and `422` are bugs, not blips. Both are logged as warnings on the
  `datum` logger. Since `record` buffers, watch the log rather than a return
  value.
- `record` never raises for size, and never drops for it either. However much
  you hand over, it ships in 10 000-sample requests.

There is still a one-shot escape hatch for a caller that wants the request now
and accepts paying for it:

```python
status = datum.send(batch)  # skips the buffer, returns the status
```

`send` raises `ValueError` above 10 000 samples in one call, and returns `0`
for nothing to send. `record` has no such limit — it chunks.

| Status | What happened |
|---|---|
| 200 | stored |
| 401 | JWT missing, expired, unsigned, or signed by the wrong key |
| 403 | the token may not write, or names no `resource_id` |
| 422 | a name or value datum will not store |
| 413 | more than 10 000 samples reached datum |
| 503 | datum is up, its store is not |
| 0 | never got there |

The bad case is silence. An expired token gives 401, metrics stop, and the
config still looks fine. Check the status, or have an alert for a source going
quiet.

## The token decides who you are

Your JWT carries `resource_id`, the machine the metrics came from, and
`access`, which must include `write`. Datum stamps every row with that id. You
cannot send your own: there is no field for it, and one hidden in your labels is
dropped. That is what stops one host reporting as another.

```python
batch.gauge("cpu", 1.2, "percent", service="web")
# stored as:
# metric=pilot_process_cpu_percent  resource_id=vm-abc123
# labels={"service": "web"}         value=1.2
```

## Limits

| Limit | Value | Enforced by |
|---|---|---|
| samples per request | 10 000 | the client, before sending, and datum on both write paths |
| metric name length | 200 | datum |
| labels per sample | none yet | — |
| label value length | none yet | — |
| request size | none yet | — |

Two gaps worth knowing about. **`NaN` and `Inf` are not rejected**, so a divide
by zero in a producer lands in the store as a number nobody can chart. Check
your own arithmetic. And the sample cap is checked after the whole body is
read, so it bounds what is stored, not what is received.

Metric and label names must match `^[a-z_][a-z0-9_]*$` here, which is stricter
than what datum accepts. The client refuses them before they are sent.
