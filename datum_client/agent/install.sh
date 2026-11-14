#!/usr/bin/env bash
# Installs the Datum agent: Vector with the machine metrics and journal lines of the telemetry
# contract, plus an optional role file of the owner. Runs as root over SSH. Idempotent.
#
# DATUM_ENDPOINT, DATUM_TOKEN  Written to /etc/vector/datum.env. Leave them out when the owner
#                              writes that file itself.
# PRODUCT                      The `product` of each log line, such as atlas or cargo.
# SEND_METRICS                 true or false. A host sets false, because metald sends its metrics.
# INFO_UNITS                   systemd units whose info lines are sent too, such as metal.service.
# ROLE_UNITS                   systemd units that the role file reads itself.
# ROLE_CONFIG                  The role file. Its transforms are named metrics_* and logs_*.
# ROLE_ENVIRONMENT             KEY=VALUE lines that the role file reads, such as a scrape token.
# DATUM_PROXY                  An HTTP proxy for the writes to Datum.

set -euo pipefail

: "${PRODUCT:?PRODUCT is required}"
SEND_METRICS=${SEND_METRICS:-true}

vector_version=0.59.0
vector_amd64_sha256=1dd9f52bda25d2364ab04d496a3b7edbc775b1a3cd02d9985eb81f3c174a245f
vector_arm64_sha256=5779afdc9abf08373c09b6656c933c8e1da34926c9b646cd80e76d96d7d64133
config_dir=/etc/vector/datum.d

if [ "$(id -u)" -ne 0 ]; then
	echo "the Datum agent install must run as root" >&2
	exit 1
fi

if [ "$(vector --version 2>/dev/null | cut -d' ' -f2)" != "$vector_version" ]; then
	echo "==> install Vector $vector_version"
	architecture=$(dpkg --print-architecture)
	case "$architecture" in
		amd64) expected_sha256=$vector_amd64_sha256 ;;
		arm64) expected_sha256=$vector_arm64_sha256 ;;
		*) echo "Vector has no package for $architecture" >&2; exit 1 ;;
	esac
	package=$(mktemp --suffix=.deb)
	trap 'rm -f "$package"' EXIT
	curl -fsSL -o "$package" "https://github.com/vectordotdev/vector/releases/download/v$vector_version/vector_$vector_version-1_$architecture.deb"
	echo "$expected_sha256  $package" | sha256sum -c --quiet
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$package"
fi
usermod -aG systemd-journal vector

# The files hold tokens, so only root and Vector can read them.
umask 027
install -d -m 750 -g vector "$config_dir"

if [ -n "${DATUM_TOKEN:-}" ]; then
	: "${DATUM_ENDPOINT:?DATUM_ENDPOINT is required with DATUM_TOKEN}"
	printf 'DATUM_ENDPOINT=%s\nDATUM_TOKEN=%s\n' "${DATUM_ENDPOINT%/}" "$DATUM_TOKEN" > /etc/vector/datum.env.next
	mv /etc/vector/datum.env.next /etc/vector/datum.env
fi
if ! grep -q '^DATUM_TOKEN=' /etc/vector/datum.env 2>/dev/null; then
	echo "/etc/vector/datum.env has no DATUM_TOKEN" >&2
	exit 1
fi
printf '%s\n' "${ROLE_ENVIRONMENT:-}" > /etc/vector/role.env
chgrp vector /etc/vector/datum.env /etc/vector/role.env

yaml_list() {
	local items=() item
	for item in $1; do items+=("\"$item\""); done
	local IFS=,
	echo "[${items[*]}]"
}

sink_proxy() {
	if [ -n "${DATUM_PROXY:-}" ]; then
		printf '    proxy:\n      enabled: true\n      https: "%s"\n' "$DATUM_PROXY"
	fi
}

# The contract allows 12 writes a minute on each route from one resource, and 10,000 items in one.
write_logs_config() {
	cat <<VECTOR
data_dir: /var/lib/vector

sources:
  journal_warning:
    type: journald
    since_now: true
    exclude_units: $(yaml_list "${INFO_UNITS:-} ${ROLE_UNITS:-}")
    include_matches:
      PRIORITY: ["0", "1", "2", "3", "4"]
VECTOR
	if [ -n "${INFO_UNITS:-}" ]; then
		cat <<VECTOR
  journal_info:
    type: journald
    since_now: true
    include_units: $(yaml_list "$INFO_UNITS")
    include_matches:
      PRIORITY: ["0", "1", "2", "3", "4", "5", "6"]
VECTOR
	fi
	cat <<VECTOR

transforms:
  logs_journal:
    type: remap
    inputs: [journal_*]
    # Datum refuses a whole batch for one line outside the contract.
    drop_on_error: true
    source: |
      levels = ["emergency", "alert", "critical", "error", "warning", "notice", "info", "debug"]
      aliases = {"warn": "warning", "err": "error", "crit": "critical", "emerg": "emergency", "fatal": "critical", "panic": "emergency", "trace": "debug"}
      unit = string(._SYSTEMD_UNIT) ?? string(.SYSLOG_IDENTIFIER) ?? "system"
      service = replace(replace(unit, r'\.service\$', ""), r'[^a-zA-Z0-9_./-]', "_")
      if ._TRANSPORT == "kernel" {
        service = "kernel"
      }
      level = string(get(levels, [to_int(string(.PRIORITY) ?? "6") ?? 6]) ?? "info") ?? "info"
      message = to_string(.message) ?? ""
      attributes = {}
      # A service that logs JSON, such as metald or Datum, gives its own level, message and fields.
      record, error = parse_json(message)
      if error == null && is_object(record) {
        record = object!(record)
        text = record.msg || record.message
        if text != null {
          message = to_string(text) ?? encode_json(text)
          named = downcase(to_string(record.level) ?? level)
          named = string(get(aliases, [named]) ?? named) ?? named
          if includes(levels, named) {
            level = named
          }
          del(record.time)
          del(record.ts)
          del(record.level)
          del(record.msg)
          del(record.message)
          fields = map_keys(compact(record)) -> |key| { replace(key, r'[^a-zA-Z0-9_]', "_") }
          attributes = map_values(fields) -> |value| { to_string(value) ?? encode_json(value) }
        }
      }
      # Go writes a panic or a fatal runtime error as plain text on stderr.
      if match(message, r'^(panic|fatal error): ') {
        level = "critical"
      }
      stamp = .timestamp
      if !is_timestamp(stamp) {
        stamp = now()
      }
      . = {
        "ts": format_timestamp!(stamp, "%Y-%m-%dT%H:%M:%S%.3fZ"),
        "product": "$PRODUCT",
        "service": service,
        "source": "journal",
        "level": level,
        "message": truncate(message, 8192),
        "attributes": attributes,
      }

sinks:
  datum_logs:
    type: http
    inputs: ["logs_*"]
    uri: "\${DATUM_ENDPOINT}/v1/logs/ingest"
    method: post
    encoding:
      codec: json
    batch:
      max_events: 5000
      timeout_secs: 10
    request:
      concurrency: 1
      rate_limit_num: 10
      rate_limit_duration_secs: 60
      headers:
        Authorization: "Bearer \${DATUM_TOKEN}"
    healthcheck:
      enabled: false
    buffer:
      type: disk
      max_size: 268435488
      when_full: drop_newest
VECTOR
	sink_proxy
}

write_metrics_config() {
	cat <<'VECTOR'
sources:
  system_raw:
    type: host_metrics
    scrape_interval_secs: 30
    collectors: [cpu, memory, filesystem, load, network, disk, host]
    filesystem:
      filesystems:
        excludes: [autofs, binfmt_misc, bpf, cgroup, cgroup2, configfs, debugfs, devpts, devtmpfs, efivarfs, fusectl, hugetlbfs, mqueue, nsfs, overlay, proc, pstore, ramfs, rpc_pipefs, securityfs, selinuxfs, squashfs, sysfs, tmpfs, tracefs]
      mountpoints:
        excludes: ["/proc/*", "/sys/*", "/dev/*", "/run/*", "/snap/*"]
    network:
      devices:
        excludes: [lo, "veth*", "docker*"]
    disk:
      devices:
        excludes: ["loop*", "ram*"]

transforms:
  metrics_system:
    type: remap
    inputs: [system_raw]
    drop_on_abort: true
    drop_on_error: true
    source: |
      names = {
        "boot_time": "system_boot_time_seconds",
        "logical_cpus": "system_cpu_count",
        "cpu_seconds_total": "system_cpu_seconds_total",
        "memory_total_bytes": "system_memory_total_bytes",
        "memory_available_bytes": "system_memory_available_bytes",
        "memory_swap_used_bytes": "system_memory_swap_used_bytes",
        "filesystem_total_bytes": "system_filesystem_size_bytes",
        "filesystem_free_bytes": "system_filesystem_available_bytes",
        "disk_read_bytes_total": "system_disk_read_bytes_total",
        "disk_written_bytes_total": "system_disk_written_bytes_total",
        "network_receive_bytes_total": "system_network_receive_bytes_total",
        "network_transmit_bytes_total": "system_network_transmit_bytes_total",
        "network_receive_errs_total": "system_network_receive_errors_total",
        "network_transmit_errs_total": "system_network_transmit_errors_total",
      }
      windows = {"load1": "1m", "load5": "5m", "load15": "15m"}
      source_name = string!(.name)
      tags = {}
      window = get(windows, [source_name]) ?? null
      name = get(names, [source_name]) ?? null
      if window != null {
        tags.window = window
        name = "system_load_average"
      } else if name == null {
        abort
      }
      if starts_with(source_name, "cpu_") {
        tags.cpu = .tags.cpu
        tags.mode = if .tags.mode == "io_wait" { "iowait" } else { .tags.mode }
      } else if starts_with(source_name, "filesystem_") {
        tags.mountpoint = .tags.mountpoint
        tags.device = .tags.device
        tags.type = .tags.filesystem
      } else if starts_with(source_name, "disk_") {
        tags.device = .tags.device
      } else if starts_with(source_name, "network_") {
        tags.interface = .tags.device
      }
      .name = name
      .tags = compact(tags)
      del(.namespace)

sinks:
  datum_metrics:
    type: prometheus_remote_write
    inputs: ["metrics_*"]
    endpoint: "${DATUM_ENDPOINT}/v1/ingest/remote"
    auth:
      strategy: bearer
      token: "${DATUM_TOKEN}"
    batch:
      max_events: 5000
      timeout_secs: 30
    request:
      concurrency: 1
      rate_limit_num: 10
      rate_limit_duration_secs: 60
    healthcheck:
      enabled: false
    buffer:
      type: disk
      max_size: 268435488
      when_full: drop_newest
VECTOR
	sink_proxy
}

rm -rf "$config_dir.next"
install -d -m 750 -g vector "$config_dir.next"
write_logs_config > "$config_dir.next/logs.yaml"
if [ "$SEND_METRICS" = true ]; then
	write_metrics_config > "$config_dir.next/metrics.yaml"
fi
if [ -n "${ROLE_CONFIG:-}" ]; then
	printf '%s\n' "$ROLE_CONFIG" > "$config_dir.next/role.yaml"
fi
chgrp -R vector "$config_dir.next"

set -a
. /etc/vector/datum.env
. /etc/vector/role.env
set +a
vector validate --no-environment "$config_dir.next"/*.yaml
rm -rf "$config_dir"
mv "$config_dir.next" "$config_dir"

# The package unit reads /etc/vector/vector.yaml and stops restarting after 5 failures in 10 seconds.
install -d /etc/systemd/system/vector.service.d
cat > /etc/systemd/system/vector.service.d/datum.conf <<UNIT
[Unit]
StartLimitIntervalSec=0

[Service]
EnvironmentFile=/etc/vector/datum.env
EnvironmentFile=/etc/vector/role.env
ExecStartPre=
ExecStartPre=/usr/bin/vector validate --config-dir $config_dir
ExecStart=
ExecStart=/usr/bin/vector --config-dir $config_dir
ExecReload=
ExecReload=/bin/kill -HUP \$MAINPID
RestartSec=5
UNIT
chmod 644 /etc/systemd/system/vector.service.d/datum.conf
systemctl daemon-reload

# Vector reads the token once at start, so a new token needs a restart.
systemctl enable --quiet vector
systemctl restart vector
systemctl is-active --quiet vector
echo "==> Datum agent sends to $(grep '^DATUM_ENDPOINT=' /etc/vector/datum.env | cut -d= -f2-)"
