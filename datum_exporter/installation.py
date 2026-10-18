from __future__ import annotations

import hashlib
import os
import platform
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "0.59.0"
CHECKSUMS = {
    "x86_64": "a8dbc43c18ae25d0b23a712c9262f3aad904e352b1db5e7db1d9c4aecab59496",
    "aarch64": "9a666b266168fc44a086741ed92a01880d5ae6e08e4bb49ef40a54e47cc5a8a0",
}


def check_scope(scope: str) -> None:
    if scope not in {"user", "system"}:
        raise ValueError("scope must be user or system")
    if scope == "system" and os.geteuid() != 0:
        raise PermissionError(
            "system installation requires root; use scope='user' for rootless use"
        )


def config_directory(scope: str = "user") -> Path:
    if scope == "system":
        return Path("/etc/datum-exporter")
    if scope != "user":
        raise ValueError("scope must be user or system")
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "datum-exporter"


def binary_path(scope: str = "user") -> Path:
    if scope == "system":
        return Path("/usr/local/bin/vector")
    if scope != "user":
        raise ValueError("scope must be user or system")
    return Path.home() / ".local/bin/vector"


def install_binary(*, scope: str = "user", destination: str | Path | None = None) -> Path:
    """Install the pinned Linux musl binary without a package manager."""
    check_scope(scope)
    if platform.system() != "Linux":
        raise ValueError("automatic binary installation supports Linux only")
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if machine not in CHECKSUMS:
        raise ValueError(f"unsupported architecture: {machine}")
    target = f"{machine}-unknown-linux-musl"
    url = f"https://install.datadoghq.com/vector/{VERSION}/vector-{VERSION}-{target}.tar.gz"
    destination = Path(destination or binary_path(scope)).expanduser().absolute()
    with tempfile.TemporaryDirectory(prefix="datum-vector-") as temporary:
        archive = Path(temporary) / "vector.tar.gz"
        with urlopen(url, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with archive.open("rb") as downloaded:
            digest = hashlib.file_digest(downloaded, "sha256").hexdigest()
        if digest != CHECKSUMS[machine]:
            raise ValueError("Vector archive checksum mismatch")
        _install_archive(archive, target, destination)
    return destination


def _install_archive(archive: Path, target: str, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as package:
        member = package.getmember(f"./vector-{target}/bin/vector")
        if not member.isfile():
            raise ValueError("Vector archive binary must be a regular file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".vector-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as output, package.extractfile(member) as source:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fchmod(output.fileno(), 0o755)
                os.fsync(output.fileno())
            os.replace(name, destination)
        finally:
            Path(name).unlink(missing_ok=True)
