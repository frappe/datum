# Datum agent

The Datum agent is Vector with one configuration. It sends the telemetry that Beacon reads from each machine: the machine metrics, the journal lines at `warning` and higher, and the data of a role. The names and labels are in `docs/telemetry.md` of [frappe/beacon](https://github.com/frappe/beacon).

## Install

`datum_client/agent/install.sh` installs a fixed Vector version, checks its SHA-256, and writes the configuration to `/etc/vector/datum.d/`. It runs as root over SSH, and it can run again with no other effect. Atlas and Cargo get the script from `datum_client.agent.get_install_script()`.

| Variable | What it does |
| --- | --- |
| `DATUM_ENDPOINT`, `DATUM_TOKEN` | Written to `/etc/vector/datum.env`. Leave them out when the owner writes that file. |
| `PRODUCT` | Required. The `product` of each log line, such as `atlas` or `cargo`. |
| `SEND_METRICS` | `true` or `false`. A host sets `false`, because metald sends its metrics. |
| `INFO_UNITS` | The systemd units whose `info` lines are sent too. |
| `ROLE_UNITS` | The systemd units that the role file reads itself. |
| `ROLE_CONFIG` | The role file. A machine with more than one role gives one YAML document for each, split by `---`. |
| `ROLE_ENVIRONMENT` | `KEY=VALUE` lines that the role file reads, such as a scrape token. |
| `LOG_GROUPS` | More groups whose log files a role file reads, such as `clickhouse`. Vector is always in `adm`. |
| `DATUM_PROXY` | An HTTP proxy for the writes to Datum. |

The script validates the new configuration before it replaces the old one.

## What it sends

- The machine metrics, each 60 seconds.
- The journal lines at `warning` and higher, and the `info` lines of `INFO_UNITS`. It sends at most 1,000 lines each minute for each service. The other lines stay in the local journal.
- `datum_agent_dropped_events_total{component, reason}`: the events that the agent drops. `reason` is `cap` (over the log limit), `buffer_full` or `error`. A line that a role file drops on purpose does not count.

A JSON journal line gives its fields as attributes. A name that does not start with a letter or `_` gets a `_` in front, and only the first 64 names stay, so one line cannot make Datum refuse its batch.

## Role files

A role file adds the data of one program. Its transforms are named `metrics_*` and `logs_*`, and the agent sends them with the rest. `get_role_config(name)` returns a role file from `datum_client/agent/roles/` for a program that many machines run, such as `nginx` for the nginx error log at `error` and higher.

Each role file, and `logs.yaml`, has a `tests:` block. Test it with `vector test`.
