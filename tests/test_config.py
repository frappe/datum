import pytest

from datum import Settings

REQUIRED = {"DATUM_CLICKHOUSE_HOST": "clickhouse.local", "DATUM_USER_PASSWORD": "pw"}


@pytest.fixture(autouse=True)
def no_ambient_environment(monkeypatch):
    """A developer's own env must not decide what a test asserts."""
    for variable in (*REQUIRED, "DATUM_CLICKHOUSE_PORT", "DATUM_CLICKHOUSE_USER", "DATUM_TIMEOUT"):
        monkeypatch.delenv(variable, raising=False)


def configure(monkeypatch, **environment):
    for variable, value in {**REQUIRED, **environment}.items():
        monkeypatch.setenv(variable, value)


def test_the_env_file_is_what_the_service_connects_with(monkeypatch):
    configure(monkeypatch, DATUM_CLICKHOUSE_USER="datum", DATUM_CLICKHOUSE_PORT="9999")
    settings = Settings.from_env()

    assert settings.host == "clickhouse.local"
    assert settings.username == "datum"
    assert settings.password == "pw"
    assert settings.port == 9999


def test_a_missing_host_is_refused(monkeypatch):
    configure(monkeypatch)
    monkeypatch.delenv("DATUM_CLICKHOUSE_HOST")

    with pytest.raises(RuntimeError, match="DATUM_CLICKHOUSE_HOST"):
        Settings.from_env()


def test_a_missing_password_is_refused(monkeypatch):
    """Falling back to the empty default would start the service against ClickHouse with
    no password, rather than saying the env file is incomplete."""
    configure(monkeypatch)
    monkeypatch.delenv("DATUM_USER_PASSWORD")

    with pytest.raises(RuntimeError, match="DATUM_USER_PASSWORD"):
        Settings.from_env()


def test_an_empty_password_is_refused(monkeypatch):
    """`DATUM_USER_PASSWORD=` in an env file is not a configured password."""
    configure(monkeypatch, DATUM_USER_PASSWORD="")

    with pytest.raises(RuntimeError, match="DATUM_USER_PASSWORD"):
        Settings.from_env()
