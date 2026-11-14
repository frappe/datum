import shutil
import subprocess

import pytest

from datum_client.agent import get_install_script, get_role_config

# Renders the configuration with the install steps left out, as the script writes it.
RENDER = """
{functions}
write_logs_config
write_agent_config
if [ "$SEND_METRICS" = true ]; then write_metrics_config; fi
"""


def render(**environment: str) -> str:
    script = get_install_script()
    functions = script[script.index("yaml_list() {") : script.index('rm -rf "$config_dir.next"')]
    result = subprocess.run(
        ["bash", "-c", RENDER.format(functions=functions)],
        env={"PATH": "/usr/bin:/bin", **environment},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_the_script_is_valid_bash():
    subprocess.run(["bash", "-n"], input=get_install_script(), text=True, check=True)


def test_a_host_sends_logs_the_info_lines_of_its_main_service_and_its_drops():
    config = render(PRODUCT="metal", SEND_METRICS="false", INFO_UNITS="metal.service")

    assert 'exclude_units: ["metal.service"]' in config
    assert 'include_units: ["metal.service"]' in config
    assert '"product": "metal"' in config
    assert "host_metrics" not in config
    assert "datum_agent_dropped_events_total" in config


def test_a_role_unit_is_left_to_the_role_file():
    config = render(PRODUCT="cargo", SEND_METRICS="true", ROLE_UNITS="garage.service")

    assert 'exclude_units: ["garage.service"]' in config
    assert "journal_info" not in config
    assert "system_memory_available_bytes" in config


def test_the_sinks_read_every_role_transform():
    config = render(PRODUCT="atlas", SEND_METRICS="true")

    assert 'inputs: ["logs_*"]' in config
    assert 'inputs: ["metrics_*"]' in config


def test_a_proxy_goes_on_each_sink():
    config = render(PRODUCT="metal", SEND_METRICS="true", DATUM_PROXY="http://127.0.0.1:9003")

    assert config.count('https: "http://127.0.0.1:9003"') == 2


def test_each_role_gets_its_own_file(tmp_path):
    script = get_install_script()
    functions = script[
        script.index("write_role_files() {") : script.index('rm -rf "$config_dir.next"')
    ]
    subprocess.run(
        ["bash", "-c", functions + f'write_role_files "{tmp_path}"'],
        env={
            "PATH": "/usr/bin:/bin",
            "ROLE_CONFIG": "sources:\n  garage: {}\n---\nsources:\n  nginx: {}",
        },
        check=True,
    )

    assert (tmp_path / "role-1.yaml").read_text() == "sources:\n  garage: {}\n"
    assert (tmp_path / "role-2.yaml").read_text() == "sources:\n  nginx: {}\n"


def test_a_shared_role_adds_no_sink():
    assert "sinks:" not in get_role_config("nginx")


@pytest.mark.skipif(shutil.which("vector") is None, reason="needs the vector binary")
def test_the_log_transform_passes_its_vector_tests(tmp_path):
    config = render(PRODUCT="metal", SEND_METRICS="false")
    logs = config[: config.index("\nsources:", config.index("sources:") + 1)]
    (tmp_path / "logs.yaml").write_text(logs)

    subprocess.run(["vector", "test", str(tmp_path / "logs.yaml")], check=True)
