from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from datum.api.internals.schemas import LogLineRequest
from datum_client import vector

TESTS = Path(__file__).parent / "vector" / "datum.test.yaml"


@pytest.fixture
def vector_binary():
    binary = shutil.which("vector")
    if binary is None:
        if os.environ.get("VECTOR_REQUIRE_TESTS") == "1":
            pytest.fail("CI requires Vector on PATH")
        pytest.skip("Vector is not installed")
    return binary


def test_install_writes_private_files_and_rotates_credentials(tmp_path):
    config = vector.install(tmp_path, endpoint="https://datum.example.com/", token="first")
    assert config == tmp_path / "datum.yaml"
    assert (tmp_path / "datum/endpoint").read_text() == "https://datum.example.com"
    assert (tmp_path / "datum/token").read_text() == "first"
    assert stat.S_IMODE((tmp_path / "datum").stat().st_mode) == 0o700
    for path in (config, tmp_path / "datum/endpoint", tmp_path / "datum/token"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    vector.install(tmp_path, endpoint="http://127.0.0.1:8000", token="second")
    assert (tmp_path / "datum/token").read_text() == "second"
    assert (tmp_path / "datum/endpoint").read_text() == "http://127.0.0.1:8000"
    assert config.read_bytes() == Path(vector.__file__).with_name("datum.yaml").read_bytes()


@pytest.mark.parametrize(
    "endpoint",
    [
        "datum.example.com",
        "ftp://example.com",
        "https://",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?q=x",
        "https://example.com#x",
        "https://example.com:bad",
        "https://example.com:65536",
        "https://example.com:0",
        "https://exa mple.com",
    ],
)
def test_invalid_endpoint_leaves_existing_installation_untouched(tmp_path, endpoint):
    vector.install(tmp_path, endpoint="https://datum.example.com", token="original")
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(ValueError):
        vector.install(tmp_path, endpoint=endpoint, token="replacement")
    assert {
        str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before


@pytest.mark.parametrize("token", ["", "token\n", "two tokens"])
def test_invalid_token_creates_no_files(tmp_path, token):
    with pytest.raises(ValueError):
        vector.install(tmp_path / "new", endpoint="https://datum.example.com", token=token)
    assert not (tmp_path / "new").exists()


def test_failed_replacement_keeps_the_previous_file_and_removes_temporary_files(tmp_path):
    vector.install(tmp_path, endpoint="https://datum.example.com", token="original")
    with (
        patch.object(vector.os, "replace", side_effect=OSError("disk error")),
        pytest.raises(OSError, match="disk error"),
    ):
        vector.install(tmp_path, endpoint="https://new.example.com", token="new")
    assert (tmp_path / "datum/endpoint").read_text() == "https://datum.example.com"
    assert sorted(path.name for path in (tmp_path / "datum").iterdir()) == ["endpoint", "token"]


def test_vector_specifications(tmp_path, vector_binary):
    config = vector.install(tmp_path, endpoint="http://127.0.0.1:1", token="test")
    app = tmp_path / "app.yaml"
    app.write_text("""sources:
  app:
    type: stdin
transforms:
  app_to_datum:
    type: remap
    inputs: [app]
    source: '.product = "app"'
""")
    result = subprocess.run(
        [vector_binary, "test", str(config), str(app), str(TESTS)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_http_payloads_pass_datum_validation_and_secrets_resolve_from_working_directory(
    tmp_path, vector_binary
):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers["Authorization"], body))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = threading.Thread(target=receiver.serve_forever, daemon=True)
    worker.start()
    directory = tmp_path / "working"
    config = vector.install(
        directory, endpoint=f"http://127.0.0.1:{receiver.server_port}", token="test-token"
    )
    config = config.rename(tmp_path / "datum.yaml")
    app = tmp_path / "app.yaml"
    app.write_text("""sources:
  app:
    type: stdin
    decoding:
      codec: json
transforms:
  app_to_datum:
    type: remap
    inputs: [app]
    source: '.'
""")
    records = [
        {},
        {"message": "", "msg": "fallback", "": "empty key", "1key": True},
        {"message": "é" * 9000, "product": "a b", "level": "9invalid"},
        {"timestamp": "invalid", "message": "bad time"},
        {"timestamp": "2026-10-14T10:00:00+05:30", "message": "offset"},
        {"message": "attributes", **{f"key_{index}": index for index in range(100)}},
        {"message": 'escaping\n\t\u0001"\\', "a b": 1, "a-b": 2, "nested": {"x": 1}},
    ]
    records.extend({"message": f"line {index}"} for index in range(10_001))
    try:
        result = subprocess.run(
            [vector_binary, "--config", str(app), "--config", str(config)],
            cwd=directory,
            input="".join(json.dumps(record) + "\n" for record in records),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        lines = []
        for path, authorization, body in received:
            assert path == "/v1/logs/ingest"
            assert authorization == "Bearer test-token"
            batch = LogLineRequest.model_validate_json(body)
            assert len(batch.lines) <= 10_000
            assert len(body) < 1024 * 1024
            lines.extend(batch.lines)
        assert len(lines) == len(records)
        assert lines[1].message == "fallback"
        assert len(lines[2].message) == 8192
        assert lines[4].ts.isoformat() == "2026-10-14T04:30:00+00:00"
        assert len(lines[5].attributes) == 64
    finally:
        receiver.shutdown()
        receiver.server_close()
        worker.join()


def test_file_source_replays_unaccepted_records_after_restart(tmp_path, vector_binary):
    expected = {f"recovery line {index}" for index in range(100)}
    rejected = set()
    accepted = set()
    rejected_batch = threading.Event()
    recovered = threading.Event()
    accepting = threading.Event()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            messages = {line.message for line in LogLineRequest.model_validate_json(body).lines}
            if accepting.is_set():
                accepted.update(messages)
                status = 200
            else:
                rejected.update(messages)
                status = 503
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            if status == 503:
                rejected_batch.set()
            elif expected <= accepted:
                recovered.set()

        def log_message(self, *args):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = threading.Thread(target=receiver.serve_forever, daemon=True)
    worker.start()
    process = None
    try:
        vector.install(tmp_path, endpoint=f"http://127.0.0.1:{receiver.server_port}", token="test")
        data = tmp_path / "data"
        data.mkdir()
        logfile = tmp_path / "app.log"
        logfile.write_text("".join(message + "\n" for message in sorted(expected)))
        original = logfile.read_bytes()
        (tmp_path / "app.yaml").write_text(
            json.dumps(
                {
                    "data_dir": str(data),
                    "sources": {
                        "app": {"type": "file", "include": [str(logfile)], "read_from": "beginning"}
                    },
                    "transforms": {
                        "app_to_datum": {"type": "remap", "inputs": ["app"], "source": "."}
                    },
                }
            )
        )
        command = [
            vector_binary,
            "--threads",
            "2",
            "--graceful-shutdown-limit-secs",
            "2",
            "--config",
            "app.yaml",
            "--config",
            "datum.yaml",
        ]
        with (tmp_path / "vector.log").open("w+") as output:
            process = subprocess.Popen(command, cwd=tmp_path, stdout=output, stderr=output)
            assert rejected_batch.wait(20), (tmp_path / "vector.log").read_text()
            process.terminate()
            process.wait(timeout=10)
            assert rejected == expected
            assert not accepted
            assert any(path.is_file() for path in data.rglob("*")), "no persisted checkpoint"

            accepting.set()
            process = subprocess.Popen(command, cwd=tmp_path, stdout=output, stderr=output)
            assert recovered.wait(20), (tmp_path / "vector.log").read_text()
            process.terminate()
            process.wait(timeout=10)
            assert accepted == expected
            assert logfile.read_bytes() == original
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        receiver.shutdown()
        receiver.server_close()
        worker.join(timeout=5)
