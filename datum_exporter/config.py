from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

from datum_client.vector import _write_private


class Config:
    """Edit an application YAML config; mappings merge and lists replace."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        self.data = yaml.safe_load(self.path.read_text()) if self.path.exists() else {}
        if self.data is None:
            self.data = {}
        if not isinstance(self.data, dict):
            raise TypeError("config must be a YAML mapping")

    def set(self, path: list[str], value: object) -> Config:
        _check_keys(path)
        candidate = copy.deepcopy(self.data)
        node = candidate
        for key in path[:-1]:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                raise TypeError(f"{key} is not a mapping")
        node[path[-1]] = copy.deepcopy(value)
        self.data = candidate
        return self

    def remove(self, path: list[str]) -> Config:
        _check_keys(path)
        node = self.data
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
        return self

    def component(self, section: str, name: str, values: dict, *, replace: bool = False) -> Config:
        if section not in {"sources", "transforms", "sinks"}:
            raise ValueError("section must be sources, transforms or sinks")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            raise ValueError("invalid component name")
        if name in {"datum", "datum_line"}:
            raise ValueError("component name is reserved by Datum")
        if not isinstance(values, dict):
            raise TypeError("component must be a mapping")
        components = self.data.get(section, {})
        if not isinstance(components, dict):
            raise TypeError(f"{section} must be a mapping")
        existing = components.get(name, {})
        value = copy.deepcopy(values) if replace else _merge(existing, values)
        return self.set([section, name], value)

    def file_source(self, name: str, include: list[str], *, product: str) -> Config:
        if (
            not isinstance(include, list)
            or not include
            or not all(isinstance(path, str) and path for path in include)
        ):
            raise ValueError("include must contain log file paths")
        if not isinstance(product, str):
            raise TypeError("product must be a string")
        # Escape Vector's environment expansion inside the VRL literal.
        product_literal = json.dumps(product, ensure_ascii=False).replace("$", "$$")
        candidate = copy.copy(self)
        candidate.component(
            "sources", name, {"type": "file", "include": include, "read_from": "end"}
        )
        candidate.component(
            "transforms",
            f"{name}_to_datum",
            {"type": "remap", "inputs": [name], "source": f".product = {product_literal}"},
        )
        self.data = candidate.data
        return self

    def save(self, *, binary: str | Path | None = None) -> Path:
        """Atomically save; when supplied, Vector validates before replacing the file."""
        content = yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if binary is not None:
            descriptor, name = tempfile.mkstemp(suffix=".yaml", dir=self.path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "w") as output:
                    output.write(content)
                validate(self.path.parent, binary=binary, app=temporary)
            finally:
                temporary.unlink(missing_ok=True)
        _write_private(self.path, content)
        return self.path


def _check_keys(path: list[str]) -> None:
    if (
        not isinstance(path, list)
        or not path
        or any(not isinstance(key, str) or not key for key in path)
    ):
        raise ValueError("path must be a list of nonempty string keys")


def _merge(existing: dict, updates: dict) -> dict:
    result = copy.deepcopy(existing)
    if not isinstance(result, dict):
        raise TypeError("existing component must be a mapping")
    for key, value in updates.items():
        result[key] = (
            _merge(result[key], value)
            if isinstance(result.get(key), dict) and isinstance(value, dict)
            else copy.deepcopy(value)
        )
    return result


def validate(directory: str | Path, *, binary: str | Path, app: Path | None = None) -> None:
    directory = Path(directory).expanduser().absolute()
    binary = os.fspath(binary)
    if os.path.dirname(binary):
        binary = str(Path(binary).expanduser().absolute())
    subprocess.run(
        [
            binary,
            "validate",
            "--no-environment",
            "--resolve-secrets",
            str(app or directory / "app.yaml"),
            str(directory / "datum.yaml"),
        ],
        cwd=directory,
        check=True,
        timeout=60,
    )
