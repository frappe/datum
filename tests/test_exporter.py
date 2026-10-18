from __future__ import annotations

import io
import os
import stat
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from datum_client.vector import install
from datum_exporter import Config, Service, installation
from datum_exporter.cli import main


def test_edit_preserves_unrelated_sections_and_merges_nested_values(tmp_path):
    path = tmp_path / "app.yaml"
    path.write_text("""data_dir: /tmp/data
sources:
  app:
    type: file
    include: [old]
    multiline:
      mode: halt_before
      timeout_ms: 1000
transforms:
  custom:
    type: remap
    inputs: [app]
    source: '.'
""")
    config = Config(path)
    config.component("sources", "app", {"include": ["new"], "multiline": {"timeout_ms": 2000}})
    config.set(["sources", "app", "read_from"], "beginning").remove(
        ["sources", "app", "multiline", "mode"]
    )
    config.save()
    saved = Config(path).data
    assert saved["data_dir"] == "/tmp/data"
    assert saved["sources"]["app"] == {
        "type": "file",
        "include": ["new"],
        "multiline": {"timeout_ms": 2000},
        "read_from": "beginning",
    }
    assert saved["transforms"]["custom"]["source"] == "."
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    config.component("sources", "app", {"type": "stdin"}, replace=True).save()
    assert Config(path).data["sources"]["app"] == {"type": "stdin"}


def test_invalid_edit_does_not_mutate_config(tmp_path):
    config = Config(tmp_path / "app.yaml").set(["scalar"], "text")
    with pytest.raises(TypeError, match="not a mapping"):
        config.set(["scalar", "nested"], 1)
    assert config.data == {"scalar": "text"}
    with pytest.raises(ValueError, match="reserved"):
        config.component("sinks", "datum", {})
    with pytest.raises(ValueError, match="section"):
        config.component("unknown", "app", {})


def test_validation_failure_keeps_previous_config(tmp_path, monkeypatch):
    path = tmp_path / "app.yaml"
    path.write_text("sources: {}\n")
    config = Config(path).set(["invalid"], True)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(78, "vector")

    monkeypatch.setattr("datum_exporter.config.validate", fail)
    with pytest.raises(subprocess.CalledProcessError):
        config.save(binary="vector")
    assert path.read_text() == "sources: {}\n"
    assert list(tmp_path.iterdir()) == [path]


def test_cli_rootless_configuration_and_token_rotation(tmp_path):
    token = tmp_path / "token"
    token.write_text("first\n")
    directory = tmp_path / "exporter"
    prefix = ["--directory", str(directory)]
    init = prefix + ["init", "--endpoint", "http://localhost:8000", "--token-file", str(token)]
    assert main(init) == 0
    assert main(prefix + ["add-file", "app", "/tmp/*.log", "--product", "my-app"]) == 0
    assert main(prefix + ["set", "sources", "app", "read_from", "--value", "beginning"]) == 0
    config = (directory / "app.yaml").read_bytes()
    token.write_text("replacement")
    assert main(init) == 0
    assert (directory / "app.yaml").read_bytes() == config
    assert (directory / "datum/token").read_text() == "replacement"
    assert (directory / "data").is_dir()
    assert main(prefix + ["remove", "sources", "app", "read_from"]) == 0
    assert "read_from" not in Config(directory / "app.yaml").data["sources"]["app"]


@pytest.mark.parametrize("scope", ["user", "system"])
def test_service_lifecycle_uses_correct_scope_and_preserves_data(tmp_path, monkeypatch, scope):
    monkeypatch.setattr("datum_exporter.service.platform.system", lambda: "Linux")
    monkeypatch.setattr("datum_exporter.installation.os.geteuid", lambda: 0)
    path = tmp_path / "units" / "proof.service"
    monkeypatch.setattr(Service, "unit_path", property(lambda self: path))
    validation = Mock()
    monkeypatch.setattr("datum_exporter.service.validate", validation)
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("datum_exporter.service.subprocess.run", run)
    directory = tmp_path / "config with % and $"
    directory.mkdir()
    checkpoint = directory / "checkpoint"
    checkpoint.write_text("keep")
    service = Service(directory, binary=tmp_path / 'vector $%"', scope=scope, name="proof")
    service.install()
    unit = path.read_text()
    assert "%%" in unit and "$%%" in unit and "$$" not in unit
    assert ("WantedBy=default.target" if scope == "user" else "WantedBy=multi-user.target") in unit
    prefix = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
    assert [call.args[0] for call in run.call_args_list] == [
        prefix + ["daemon-reload"],
        prefix + ["enable", "proof.service"],
        prefix + ["restart", "proof.service"],
    ]
    service.restart()
    assert validation.call_count == 2
    service.remove()
    assert not path.exists()
    assert checkpoint.read_text() == "keep"


def test_service_refuses_foreign_unit_and_bad_names(tmp_path, monkeypatch):
    monkeypatch.setattr("datum_exporter.service.platform.system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    service = Service(tmp_path, binary="/bin/vector")
    service.unit_path.parent.mkdir(parents=True)
    service.unit_path.write_text("[Service]\nExecStart=/bin/other\n")
    with pytest.raises(ValueError, match="unrelated"):
        service.remove()
    with pytest.raises(ValueError):
        Service(tmp_path, binary="/bin/vector", name="../bad")
    with pytest.raises(ValueError, match="control"):
        Service(tmp_path, binary="/bin/vector\nInjected=yes").render()


def test_system_install_requires_root(monkeypatch):
    monkeypatch.setattr(installation.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError):
        installation.install_binary(scope="system")


def make_archive(binary=b"executable", *, symlink=False):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        member = tarfile.TarInfo("./vector-x86_64-unknown-linux-musl/bin/vector")
        if symlink:
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
        else:
            member.size = len(binary)
        archive.addfile(member, None if symlink else io.BytesIO(binary))
    return data.getvalue()


def test_binary_installer_verifies_checksum_and_atomically_replaces(tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setattr(installation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installation.platform, "machine", lambda: "x86_64")
    archive = make_archive()
    monkeypatch.setattr(installation, "urlopen", lambda *a, **k: io.BytesIO(archive))
    destination = tmp_path / "bin/vector"
    destination.parent.mkdir()
    destination.write_bytes(b"old")
    with pytest.raises(ValueError, match="checksum"):
        installation.install_binary(destination=destination)
    assert destination.read_bytes() == b"old"
    monkeypatch.setitem(installation.CHECKSUMS, "x86_64", hashlib.sha256(archive).hexdigest())
    assert installation.install_binary(destination=destination) == destination
    assert destination.read_bytes() == b"executable"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755


def test_archive_symlink_is_rejected(tmp_path):
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(make_archive(symlink=True))
    with pytest.raises(ValueError, match="regular"):
        installation._install_archive(archive, "x86_64-unknown-linux-musl", tmp_path / "vector")
    assert not (tmp_path / "vector").exists()


def test_generated_config_with_real_vector(tmp_path):
    import shutil

    binary = shutil.which("vector")
    if not binary:
        if os.environ.get("VECTOR_REQUIRE_TESTS") == "1":
            pytest.fail("Vector required")
        pytest.skip("Vector unavailable")
    install(tmp_path, endpoint="http://localhost:8000", token="test")
    config = Config(tmp_path / "app.yaml")
    config.file_source("app", [str(tmp_path / "*.log")], product='app "quotes" $HOME é')
    config.set(["data_dir"], str(tmp_path / "data")).save(binary=binary)
    previous = config.path.read_bytes()
    config.component("sources", "app", {"type": "not_a_source"})
    with pytest.raises(subprocess.CalledProcessError):
        config.save(binary=binary)
    assert config.path.read_bytes() == previous


def test_cli_reports_malformed_section_without_overwriting(tmp_path, capsys):
    path = tmp_path / "app.yaml"
    path.write_text("sources: invalid\n")
    assert main(["--directory", str(tmp_path), "add-file", "app", "*.log", "--product", "app"]) == 1
    assert "sources must be a mapping" in capsys.readouterr().err
    assert path.read_text() == "sources: invalid\n"


def test_bad_existing_config_does_not_rotate_credentials(tmp_path):
    install(tmp_path, endpoint="http://localhost:8000", token="original")
    (tmp_path / "app.yaml").write_text("data_dir: []\n")
    token = tmp_path / "replacement"
    token.write_text("new")
    assert (
        main(
            [
                "--directory",
                str(tmp_path),
                "init",
                "--endpoint",
                "http://localhost:8000",
                "--token-file",
                str(token),
            ]
        )
        == 1
    )
    assert (tmp_path / "datum/token").read_text() == "original"


def test_unit_working_directory_is_unquoted(tmp_path):
    directory = tmp_path / 'folder with % and "quotes" '
    unit = Service(directory, binary=tmp_path / "vector").render()
    assert f"WorkingDirectory={str(directory).replace('%', '%%')}/\n" in unit


def test_failed_stop_does_not_remove_unit(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("datum_exporter.service.platform.system", lambda: "Linux")
    service = Service(tmp_path, binary="/bin/vector")
    service.unit_path.parent.mkdir(parents=True)
    service.unit_path.write_text(service.render())
    monkeypatch.setattr(
        service, "_run", lambda *args, **kwargs: subprocess.CompletedProcess(["systemctl"], 1)
    )
    with pytest.raises(subprocess.CalledProcessError):
        service.remove()
    assert service.unit_path.exists()


@pytest.mark.parametrize("path", ["sources", [], [""], [1]])
@pytest.mark.parametrize("operation", ["set", "remove"])
def test_config_rejects_invalid_key_paths(tmp_path, path, operation):
    config = Config(tmp_path / "app.yaml")
    with pytest.raises(ValueError, match="list of nonempty string keys"):
        if operation == "set":
            config.set(path, "value")
        else:
            config.remove(path)
    assert config.data == {}


def test_failed_file_source_update_preserves_existing_config(tmp_path):
    config = Config(tmp_path / "app.yaml").set(["transforms"], "invalid")
    with pytest.raises(TypeError, match="transforms must be a mapping"):
        config.file_source("app", ["*.log"], product="app")
    assert config.data == {"transforms": "invalid"}


def test_cli_validation_uses_absolute_binary_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    validation = Mock()
    monkeypatch.setattr("datum_exporter.config.validate", validation)
    assert (
        main(
            [
                "--directory",
                "config",
                "--binary",
                "bin/vector",
                "add-file",
                "app",
                "*.log",
                "--product",
                "app",
                "--validate",
            ]
        )
        == 0
    )
    assert validation.call_args.kwargs["binary"] == tmp_path / "bin/vector"


@pytest.mark.parametrize("binary", ["bin/vector", "./vector", Path("bin/vector"), "vector"])
def test_library_validation_resolves_binary_before_changing_directory(
    tmp_path, monkeypatch, binary
):
    monkeypatch.chdir(tmp_path)
    executable = tmp_path / ("vector" if binary == "./vector" else "bin/vector")
    executable.parent.mkdir(exist_ok=True)
    executable.write_text(
        '#!/bin/sh\npwd > validation-cwd\nprintf "%s\\n" "$@" > validation-args\n'
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])

    config = Config("config/app.yaml").set(["sources"], {})
    config.save(binary=binary)

    directory = tmp_path / "config"
    assert Path((directory / "validation-cwd").read_text().strip()).resolve() == directory.resolve()
    arguments = (directory / "validation-args").read_text().splitlines()
    assert arguments[:3] == ["validate", "--no-environment", "--resolve-secrets"]
    assert arguments[-1] == str(directory / "datum.yaml")
    assert config.path.exists()
