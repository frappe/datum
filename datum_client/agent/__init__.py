from importlib.resources import files


def get_install_script() -> str:
    """The script that installs the Datum agent on a machine. Its header lists the variables."""
    return files(__package__).joinpath("install.sh").read_text()
