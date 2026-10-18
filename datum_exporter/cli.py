from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

from datum_client.vector import install

from .config import Config, validate
from .installation import binary_path, check_scope, config_directory, install_binary
from .service import Service


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Configure and run the Datum Vector exporter")
    command.add_argument("--scope", choices=("user", "system"), default="user")
    command.add_argument("--directory", type=Path, help="config directory")
    command.add_argument("--binary", type=Path, help="Vector executable path")
    actions = command.add_subparsers(dest="action", required=True)
    initialize = actions.add_parser("init", help="install shared config and private credentials")
    initialize.add_argument("--endpoint", required=True)
    initialize.add_argument("--token-file", required=True, help="token file, or - for stdin")
    actions.add_parser("install-binary", help="download the pinned Linux binary")
    add = actions.add_parser("add-file", help="add/update a file source and its Datum transform")
    add.add_argument("name")
    add.add_argument("include", nargs="+")
    add.add_argument("--product", required=True)
    component = actions.add_parser("component", help="merge a YAML/JSON component mapping")
    component.add_argument("section", choices=("sources", "transforms", "sinks"))
    component.add_argument("name")
    component.add_argument("--file", type=Path, required=True)
    component.add_argument("--replace", action="store_true")
    setter = actions.add_parser("set", help="set a nested value; keys are separate arguments")
    setter.add_argument("path", nargs="+")
    setter.add_argument("--value", required=True, help="YAML/JSON value")
    remove = actions.add_parser("remove", help="remove a nested key")
    remove.add_argument("path", nargs="+")
    for editor in (add, component, setter, remove):
        editor.add_argument("--validate", action="store_true", help="validate before saving")
    actions.add_parser("validate", help="validate application and shared configuration")
    service = actions.add_parser("service", help="manage a systemd service")
    service.add_argument(
        "operation", choices=("install", "start", "restart", "stop", "status", "remove")
    )
    service.add_argument("--name", default="datum-exporter")
    service.add_argument("--no-start", action="store_true", help="install without starting")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        yaml.YAMLError,
        subprocess.SubprocessError,
    ) as error:
        print(f"datum-exporter: {error}", file=sys.stderr)
        return 1


def execute(args: argparse.Namespace) -> int:
    directory = (args.directory or config_directory(args.scope)).expanduser().absolute()
    binary = (args.binary or binary_path(args.scope)).expanduser().absolute()
    check_scope(args.scope)
    if args.action == "install-binary":
        print(install_binary(scope=args.scope, destination=binary))
    elif args.action == "init":
        token = sys.stdin.read() if args.token_file == "-" else Path(args.token_file).read_text()
        config = Config(directory / "app.yaml")
        data = Path(config.data.get("data_dir", str(directory / "data")))
        install(directory, endpoint=args.endpoint, token=token.strip())
        if "data_dir" not in config.data:
            config.set(["data_dir"], str(directory / "data")).save()
        if not data.is_absolute():
            data = directory / data
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif args.action == "validate":
        validate(directory, binary=binary)
    elif args.action == "service":
        service = Service(directory, binary=binary, scope=args.scope, name=args.name)
        if args.operation == "install":
            service.install(start=not args.no_start)
        else:
            result = getattr(service, args.operation)()
            return result if isinstance(result, int) else 0
    else:
        edit(args, Config(directory / "app.yaml"), binary=binary)
    return 0


def edit(args: argparse.Namespace, config: Config, *, binary: Path) -> None:
    if args.action == "add-file":
        config.file_source(args.name, args.include, product=args.product)
    elif args.action == "component":
        config.component(
            args.section, args.name, yaml.safe_load(args.file.read_text()), replace=args.replace
        )
    elif args.action == "set":
        config.set(args.path, yaml.safe_load(args.value))
    else:
        config.remove(args.path)
    config.save(binary=binary if args.validate else None)
