from __future__ import annotations

import os
import tempfile
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit


def install(directory: str | Path, *, endpoint: str, token: str) -> Path:
    """Install the exporter and private secrets; run Vector from this directory."""
    url = urlsplit(endpoint)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "endpoint must be an HTTP(S) base URL without credentials, query or fragment"
        )
    if url.port == 0:
        raise ValueError("endpoint port must be between 1 and 65535")
    if not token or any(character.isspace() for character in token):
        raise ValueError("token must be nonempty and contain no whitespace")

    directory = Path(directory)
    secrets = directory / "datum"
    if secrets.is_symlink():
        raise ValueError("secret directory must not be a symbolic link")
    secrets.mkdir(parents=True, exist_ok=True, mode=0o700)
    secrets.chmod(0o700)
    _write_private(secrets / "endpoint", endpoint.rstrip("/"))
    _write_private(secrets / "token", token)
    config = directory / "datum.yaml"
    _write_private(config, files(__package__).joinpath("datum.yaml").read_text())
    return config


def _write_private(path: Path, content: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
