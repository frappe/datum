<h1 align="center">Datum</h1>

<p align="center">
  <strong>Telemetry service for Frappe Cloud</strong>
</p>

<p align="center">
  <a href="https://github.com/frappe/datum/actions/workflows/tests.yml"><img src="https://github.com/frappe/datum/actions/workflows/tests.yml/badge.svg?branch=main" alt="Tests"></a>
</p>

<p align="center">
  <a href="docs/api.md">API</a> ·
  <a href="docs/storage.md">Storage</a> ·
  <a href="docs/tokens.md">Tokens</a> ·
  <a href="docs/agent.md">Datum agent</a> ·
  <a href="docs/setup.md">Setup</a>
</p>

Datum takes the metrics, log lines and traces of every machine and application in one Frappe Cloud region, and writes them to the ClickHouse of that region. Each region runs one Datum. Cargo installs it on the Datum host.

Datum only writes. Readers, such as Beacon and Insights, query ClickHouse directly with their own read-only ClickHouse user. Datum keeps no queue and no buffer: when ClickHouse is down, a write fails and the sender continues.

```text
machines and applications ──> Datum (/v1, token) ──> ClickHouse <── Beacon, Insights (SQL, read-only users)
```

## How it works

| Part | What it does |
| --- | --- |
| API | Takes JSON samples, Prometheus remote write, JSON log lines and OTLP traces under `/v1`. Each request carries a token. |
| Tokens | Central, the region's Atlas and the region's Cargo sign the tokens. The token sets the `resource_id` of each row, and the sender cannot change it. |
| Storage | The migrations make the `datum` database, its tables and views, and the ClickHouse users. Datum itself makes no schema. |
| Datum agent | One Vector configuration that Atlas and Cargo install on each machine. It sends the machine metrics, the journal and the data of a role. |
| Client | `datum_client` builds valid metric names and sends batches from Python. |

## Start here

| Task | Guide |
| --- | --- |
| Send data to Datum | [API](docs/api.md) and [datum_client](datum_client/README.md) |
| Read data, or learn what each table keeps | [Storage](docs/storage.md) |
| Learn who signs a token and what Datum checks | [Tokens](docs/tokens.md) |
| Install the agent on a machine, or add a role file | [Datum agent](docs/agent.md) |
| Run Datum, locally or on a host | [Setup](docs/setup.md) |
| Make a code change | [Contributor guide](CLAUDE.md) |

The names, labels and log fields that every sender uses are in `docs/telemetry.md` of [frappe/beacon](https://github.com/frappe/beacon).

## Development

```sh
uv sync --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

[Setup](docs/setup.md#run-it-locally) tells how to run Datum on your machine with a local ClickHouse and a test token.

## Contributing

1. [Report issues in Datum](https://github.com/frappe/datum/issues)
2. [Report Security Vulnerabilities](https://frappe.io/security)

<br>
<br>
<div align="center">
	<a href="https://frappe.io" target="_blank">
		<picture>
			<source media="(prefers-color-scheme: dark)" srcset="https://frappe.io/files/Frappe-white.png">
			<img src="https://frappe.io/files/Frappe-black.png" alt="Frappe Technologies" height="28"/>
		</picture>
	</a>
</div>
