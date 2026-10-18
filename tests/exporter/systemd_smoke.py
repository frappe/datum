"""Run in a disposable Linux systemd host as root or the target login user."""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from datum_exporter import Config, Service, install, install_binary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("user", "system"), required=True)
    args = parser.parse_args()
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                (
                    self.headers["Authorization"],
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    try:
        with TemporaryDirectory(prefix="datum service % proof ") as temporary:
            directory = Path(temporary)
            binary = install_binary(scope=args.scope, destination=directory / "bin $% vector")
            subprocess.run([str(binary), "--version"], check=True)
            endpoint = f"http://127.0.0.1:{receiver.server_port}"
            install(directory, endpoint=endpoint, token="first-token")
            (directory / "data").mkdir()
            logfile = directory / "app.log"
            logfile.write_text("first record\n")
            config = Config(directory / "app.yaml")
            config.set(["data_dir"], str(directory / "data"))
            config.file_source("app", [str(logfile)], product="proof")
            config.component("sources", "app", {"read_from": "beginning"})
            config.save(binary=binary)
            service = Service(
                directory, binary=binary, scope=args.scope, name="datum-exporter-proof"
            )
            try:
                service.install(start=False)
                service.start()
                wait_for(received, "first-token", "first record")
                install(directory, endpoint=endpoint, token="rotated-token")
                service.restart()
                with logfile.open("a") as output:
                    output.write("after rotation\n")
                wait_for(received, "rotated-token", "after rotation")
                assert service.status() == 0
                service.stop()
                service.start()
                with logfile.open("a") as output:
                    output.write("after restart\n")
                wait_for(received, "rotated-token", "after restart")
                service.remove()
                assert not service.unit_path.exists()
                assert (directory / "datum/token").read_text() == "rotated-token"
                assert list((directory / "data").rglob("*")), "checkpoints were removed"
                messages = [line["message"] for _, body in received for line in body["lines"]]
                assert messages == ["first record", "after rotation", "after restart"], messages
                print(
                    f"PASS: {args.scope} install, start, delivery, rotation, restart, stop, removal"
                )
            finally:
                service.remove()
    finally:
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=5)


def wait_for(received, token, message):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if any(
            auth == f"Bearer {token}" and any(line["message"] == message for line in body["lines"])
            for auth, body in received
        ):
            return
        time.sleep(0.2)
    raise AssertionError(f"record not delivered: {message}; received={received!r}")


if __name__ == "__main__":
    main()
