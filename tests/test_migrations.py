import pytest

from datum.migrations import migrations

PASSWORDS = {"DATUM_PASSWORD": "pw-datum", "INSIGHTS_PASSWORD": "pw-insights"}


class FakeClient:
    def __init__(self):
        self.commands: list[str] = []
        self.options: dict = {}

    def command(self, statement):
        self.commands.append(statement)


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()

    def connect(**options):
        fake.options = options
        return fake

    monkeypatch.setattr(migrations.clickhouse_connect, "get_client", connect)
    return fake


@pytest.fixture(autouse=True)
def no_ambient_environment(monkeypatch):
    """A developer's own env must not decide what a test asserts."""
    for variable in ("DATUM_CLICKHOUSE_HOST", "DATUM_CLICKHOUSE_PASSWORD", "INSIGHTS_PASSWORD"):
        monkeypatch.delenv(variable, raising=False)


def apply(**overrides):
    """run_migrations with the connection arguments a test rarely cares about."""
    arguments = {"host": "h", "port": 8123, "username": "default", "password": ""}
    return migrations.run_migrations(**{**arguments, **PASSWORDS, **overrides})


def run_cli(monkeypatch, *argv, **environment):
    monkeypatch.setattr("sys.argv", ["datum-migrate", *argv])
    for variable, value in {"DATUM_CLICKHOUSE_HOST": "clickhouse.local", **environment}.items():
        monkeypatch.setenv(variable, value)
    migrations.main()


def test_migrations_run_in_name_order():
    """The numeric prefix is the ordering: ACL before the tables that reference it."""
    names = [path.name for path in migrations.get_migrations()]

    assert names == sorted(names)
    assert names[0].startswith("000")


def test_comments_are_dropped_before_splitting():
    sql = "SELECT 1; -- a comment; with a semicolon\nSELECT 2;"

    assert migrations.get_statements(sql) == ["SELECT 1", "SELECT 2"]


def test_a_trailing_statement_without_a_semicolon_still_counts():
    assert migrations.get_statements("SELECT 1") == ["SELECT 1"]


def test_a_comment_only_file_yields_nothing():
    assert migrations.get_statements("-- nothing here\n\n") == []


def test_every_placeholder_is_filled(client):
    """A leftover `${...}` would reach ClickHouse as a literal password."""
    apply()

    assert not any("${" in command for command in client.commands)


def test_an_unfilled_placeholder_is_an_error_not_a_password(client):
    """safe_substitute would leave `${INSIGHTS_PASSWORD}` as the literal password."""
    with pytest.raises(KeyError):
        migrations.run_migrations(
            host="h", port=8123, username="default", password="", DATUM_PASSWORD="pw-datum"
        )


def test_both_users_are_created_with_the_passwords_given(client):
    apply()
    created = [command for command in client.commands if command.startswith("CREATE USER")]

    assert "IDENTIFIED BY 'pw-datum'" in created[0]
    assert "IDENTIFIED BY 'pw-insights'" in created[1]


def test_datum_is_granted_no_ddl(client):
    """Migrations make the schema; the service only writes rows."""
    apply()
    granted = next(
        command
        for command in client.commands
        if command.startswith("GRANT") and "TO datum" in command
    )

    assert "CREATE" not in granted
    assert "INSERT" in granted


def test_running_applies_every_file(client):
    applied = apply()

    assert applied == [path.name for path in migrations.get_migrations()]
    assert any("CREATE TABLE IF NOT EXISTS datum.samples" in c for c in client.commands)


def test_the_acl_runs_before_the_tables(client):
    apply()

    first_user = next(i for i, c in enumerate(client.commands) if c.startswith("CREATE USER"))
    first_table = next(i for i, c in enumerate(client.commands) if "CREATE TABLE" in c)
    assert first_user < first_table


def test_the_insights_password_is_required(monkeypatch, capsys):
    """It is stored nowhere, so there is nothing to fall back to."""
    monkeypatch.setattr("sys.argv", ["datum-migrate"])

    with pytest.raises(SystemExit):
        migrations.main()

    assert "--insights-user-password" in capsys.readouterr().err


def test_connection_and_passwords_come_from_the_env_file(monkeypatch, client):
    run_cli(
        monkeypatch,
        "--insights-user-password",
        "typed",
        DATUM_CLICKHOUSE_PASSWORD="from-env",
    )

    assert client.options["host"] == "clickhouse.local"
    assert any("IDENTIFIED BY 'from-env'" in c for c in client.commands)
    assert any("IDENTIFIED BY 'typed'" in c for c in client.commands)


def test_migrations_connect_as_default_not_as_the_service_user(monkeypatch, client):
    """DATUM_CLICKHOUSE_USER is who the API connects as; `datum` does not exist yet here."""
    monkeypatch.setenv("DATUM_CLICKHOUSE_USER", "datum")
    run_cli(
        monkeypatch,
        "--insights-user-password",
        "typed",
        DATUM_CLICKHOUSE_PASSWORD="from-env",
    )

    assert client.options["username"] == "default"


def test_an_omitted_default_password_is_empty_not_none(monkeypatch, client):
    """The driver takes `password: str`; None fails auth even where there is no password."""
    run_cli(
        monkeypatch,
        "--insights-user-password",
        "typed",
        DATUM_CLICKHOUSE_PASSWORD="from-env",
    )

    assert client.options["password"] == ""


def test_the_default_password_is_used_when_given(monkeypatch, client):
    run_cli(
        monkeypatch,
        "--insights-user-password",
        "typed",
        "--default-user-password",
        "admin-pw",
        DATUM_CLICKHOUSE_PASSWORD="from-env",
    )

    assert client.options["password"] == "admin-pw"


def test_the_port_comes_from_the_environment_as_a_number(monkeypatch, client):
    run_cli(
        monkeypatch,
        "--insights-user-password",
        "typed",
        DATUM_CLICKHOUSE_PASSWORD="from-env",
        DATUM_CLICKHOUSE_PORT="9999",
    )

    assert client.options["port"] == 9999


def test_a_missing_host_fails_before_connecting(monkeypatch):
    monkeypatch.setattr("sys.argv", ["datum-migrate", "--insights-user-password", "typed"])
    monkeypatch.setenv("DATUM_CLICKHOUSE_PASSWORD", "from-env")

    with pytest.raises(SystemExit, match="DATUM_CLICKHOUSE_HOST"):
        migrations.main()


def test_a_missing_datum_password_fails_before_connecting(monkeypatch):
    """It is what the `datum` user is created with; an empty one is a passwordless user."""
    monkeypatch.setattr("sys.argv", ["datum-migrate", "--insights-user-password", "typed"])
    monkeypatch.setenv("DATUM_CLICKHOUSE_HOST", "clickhouse.local")

    with pytest.raises(SystemExit, match="DATUM_CLICKHOUSE_PASSWORD"):
        migrations.main()


@pytest.mark.parametrize(
    "password",
    ["pa'ss", "pa;ss", "pa--ss", "pa\\ss", "pa\nss"],
)
def test_a_password_that_could_rewrite_the_sql_is_refused(client, password):
    """`;` splits the statement and `--` swallows the next line, both as the migration
    user. Refused before a connection is opened, not escaped."""
    with pytest.raises(SystemExit, match="may not contain"):
        apply(INSIGHTS_PASSWORD=password)

    assert client.commands == []


def test_an_ordinary_password_with_punctuation_is_allowed(client):
    apply(INSIGHTS_PASSWORD="p@ss-w0rd_!#%^&*()+=")

    assert any("IDENTIFIED BY 'p@ss-w0rd_!#%^&*()+='" in c for c in client.commands)


def test_the_datum_password_is_held_to_the_same_rule(client):
    with pytest.raises(SystemExit, match="DATUM_PASSWORD"):
        apply(DATUM_PASSWORD="pa;ss")


def test_the_resources_version_column_resolves_below_a_second():
    """DateTime ties two writes in the same second, so ReplacingMergeTree picks
    arbitrarily and readers can see stale status."""
    schema = (migrations.DIRECTORY / "001_init_schema.sql").read_text()

    assert "updated_at  DateTime64(3, 'UTC') DEFAULT now64(3)" in schema
    assert "ReplacingMergeTree(updated_at)" in schema


def test_log_stats_follow_the_logs_table_and_are_filled_by_a_view():
    """The view watches `logs`; without it daily_log_stats would stay empty."""
    names = [path.name for path in migrations.get_migrations()]

    assert names.index("004_log_stats.sql") == names.index("003_logs.sql") + 1
    sql = (migrations.DIRECTORY / "004_log_stats.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS datum.daily_log_stats" in sql
    assert "CREATE MATERIALIZED VIEW IF NOT EXISTS datum.mv_daily_log_stats" in sql
    assert "FROM datum.logs" in sql


def test_traces_are_stamped_and_expire():
    """A span is per request, so it carries the tenant boundary and a TTL that
    nothing else in datum has."""
    names = [path.name for path in migrations.get_migrations()]

    assert names.index("005_traces.sql") == names.index("004_log_stats.sql") + 1
    sql = (migrations.DIRECTORY / "005_traces.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS datum.traces" in sql
    assert "ORDER BY (resource_id, service, ts)" in sql
    assert "TTL toDateTime(ts) + toIntervalDay(7)" in sql
    assert "INDEX idx_trace_id trace_id TYPE bloom_filter" in sql
