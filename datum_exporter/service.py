from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path

from datum_client.vector import _write_private

from .config import validate
from .installation import check_scope


class Service:
    """Manage one Vector systemd unit; removing it preserves config and checkpoints."""

    def __init__(
        self,
        directory: str | Path,
        *,
        binary: str | Path,
        scope: str = "user",
        name: str = "datum-exporter",
    ):
        if scope not in {"user", "system"}:
            raise ValueError("scope must be user or system")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
            raise ValueError("service name must be a simple name without .service")
        if name.endswith(".service"):
            raise ValueError("omit the .service suffix")
        self.directory = Path(directory).expanduser().absolute()
        self.binary = Path(binary).expanduser().absolute()
        self.scope = scope
        self.name = name + ".service"

    @property
    def unit_path(self) -> Path:
        if self.scope == "system":
            return Path("/etc/systemd/system") / self.name
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return config / "systemd/user" / self.name

    def render(self) -> str:
        binary = _quote(str(self.binary))
        _check_path(str(self.directory))
        directory = (str(self.directory) + "/").replace("%", "%%")
        target = "default.target" if self.scope == "user" else "multi-user.target"
        return (
            "# Managed by datum-exporter\n"
            "[Unit]\nDescription=Datum Vector log exporter\n\n"
            "[Service]\nType=exec\n"
            f"WorkingDirectory={directory}\n"
            f"ExecStart={binary} --threads 2 --config app.yaml --config datum.yaml\n"
            "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=60\nUMask=0077\n\n"
            f"[Install]\nWantedBy={target}\n"
        )

    def install(self, *, start: bool = True) -> Path:
        self._check_platform()
        unit = self.render()
        self._check_owned()
        validate(self.directory, binary=self.binary)
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(self.unit_path, unit)
        self.unit_path.chmod(0o644)
        self._run("daemon-reload")
        self._run("enable", self.name)
        if start:
            self._run("restart", self.name)
        return self.unit_path

    def start(self) -> None:
        self._action("start", validate_config=True)

    def restart(self) -> None:
        self._action("restart", validate_config=True)

    def stop(self) -> None:
        self._action("stop")

    def status(self) -> int:
        self._check_platform()
        return self._run("status", "--no-pager", self.name, check=False).returncode

    def remove(self) -> None:
        self._check_platform()
        self._check_owned()
        if not self.unit_path.exists():
            return
        stopped = self._run("stop", self.name, check=False)
        if stopped.returncode not in (0, 5):
            stopped.check_returncode()
        self._run("disable", self.name)
        self.unit_path.unlink()
        self._run("daemon-reload")

    def _action(self, action: str, *, validate_config: bool = False) -> None:
        self._check_platform()
        self._check_owned()
        if not self.unit_path.exists():
            raise FileNotFoundError(self.unit_path)
        if validate_config:
            validate(self.directory, binary=self.binary)
        self._run(action, self.name)

    def _check_owned(self) -> None:
        path = self.unit_path
        if path.is_symlink() or (
            path.exists() and not path.read_text().startswith("# Managed by datum-exporter\n")
        ):
            raise ValueError(f"refusing to replace or manage an unrelated unit: {path}")

    def _check_platform(self) -> None:
        if platform.system() != "Linux":
            raise ValueError("systemd service management requires Linux")
        check_scope(self.scope)

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
        command = ["systemctl"]
        if self.scope == "user":
            command.append("--user")
        return subprocess.run(command + list(arguments), check=check, timeout=90)


def _quote(value: str) -> str:
    _check_path(value)
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{value}"'


def _check_path(value: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("systemd paths cannot contain control characters")
