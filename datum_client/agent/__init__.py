from importlib.resources import files


def get_install_script() -> str:
    """The script that installs the Datum agent on a machine. Its header lists the variables."""
    return files(__package__).joinpath("install.sh").read_text()


def get_role_config(name: str) -> str:
    """A role file that the agent ships for a program that many machines run, such as nginx."""
    return files(__package__).joinpath("roles", f"{name}.yaml").read_text()
