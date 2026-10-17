import asyncio
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from pydantic import SecretStr, ValidationError

from digital_souls_core.application import CoreError
from digital_souls_core.character import AccessScope
from digital_souls_core.history import Binding
from digital_souls_core.postgres_db import PostgresConfig, PostgresDatabase, _lock_key, key

pytestmark = pytest.mark.ut


@pytest.fixture(autouse=True)
def explicit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith("PG"):
            monkeypatch.delenv(name)


def config(**changes: Any) -> PostgresConfig:
    return PostgresConfig.model_validate({"database": "core_test", "user": "core_test", **changes})


def binding(character: str = "miori") -> Binding:
    return Binding(AccessScope("synthetic-subject", "synthetic-client", "local-private"), character)


@pytest.mark.parametrize("host", ["127.0.0.1", "/tmp/core-pg", "/var/run/postgresql"])
def test_explicit_local_hosts(host: str) -> None:
    assert config(host=host).host == host


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "::1",
        "127.0.0.2",
        "example.com",
        "192.168.1.1",
        "",
        "@socket",
        "relative",
        "postgresql://127.0.0.1/core",
        "127.0.0.1,example.com",
        "/tmp/../pg",
        "/tmp//pg",
        "/tmp/pg/",
        "/tmp/pg,socket",
        "/tmp/pg=socket",
        "/tmp/pg\n",
        "/tmp/pg\x00",
    ],
)
def test_ambiguous_or_remote_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValidationError):
        config(host=host)


@pytest.mark.parametrize(
    "name",
    ["public", "pg_catalog", "pg_temp", "information_schema", "core;DROP", "", "Core", "x" * 64],
)
def test_schema_requires_dedicated_identifier(name: str) -> None:
    with pytest.raises(ValidationError):
        config(schema_name=name)


@pytest.mark.parametrize("field", ["database", "user"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        "x" * 64,
        "postgresql://example.com/private",
        "host=example.com",
        "a\x00",
        "name with spaces",
    ],
)
def test_database_and_user_cannot_encode_conninfo(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        config(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", 0),
        ("port", 65536),
        ("port", True),
        ("port", "5432"),
        ("connect_timeout", 0),
        ("connect_timeout", 31),
        ("connect_timeout", 1.5),
        ("statement_timeout_ms", 0),
        ("statement_timeout_ms", 30001),
        ("lock_timeout_ms", 0),
        ("lock_timeout_ms", 30001),
        ("unknown", True),
    ],
)
def test_strict_bounded_configuration(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        config(**{field: value})


def test_config_is_frozen_and_hides_secret_inputs() -> None:
    secret = "synthetic-password-marker"
    settings = config(password=SecretStr(secret))
    assert secret not in repr(settings)
    assert secret not in str(settings.model_dump())
    with pytest.raises(ValidationError) as failure:
        settings.host = "example.com"
    with pytest.raises(ValidationError) as failure:
        config(password=SecretStr(secret), database="postgresql://" + secret)
    assert secret not in str(failure.value)


def test_database_revalidates_constructed_config() -> None:
    unsafe = PostgresConfig.model_construct(database="core", user="core", host="example.com")
    with pytest.raises(ValidationError):
        PostgresDatabase(unsafe)


@pytest.mark.parametrize(
    "variable",
    [
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
        "PGPASSFILE",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGOPTIONS",
        "PGSSLMODE",
        "PGGSSENCMODE",
        "PGSYSCONFDIR",
    ],
)
def test_implicit_libpq_environment_refused_before_connect(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    secret = "synthetic-environment-marker"
    monkeypatch.setenv(variable, secret)
    connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(CoreError) as failure, PostgresDatabase(config()).transaction(binding()):
        pytest.fail("unexpected connection")
    assert failure.value.code == "storage_configuration"
    assert secret not in str(failure.value)
    connect.assert_not_called()


def mock_connection(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg, "connect", connect)
    return connect, connection


def test_explicit_connection_options_and_scope_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", "")
    connect, connection = mock_connection(monkeypatch)
    database = PostgresDatabase(config(schema_name="synthetic_core"))
    with database.transaction(binding()) as received:
        assert received is connection
    kwargs = connect.call_args.kwargs
    assert connect.call_args.args == ()
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["dbname"] == kwargs["user"] == "core_test"
    assert kwargs["password"] == ""
    assert kwargs["passfile"] == "/dev/null/no-passfile"
    assert kwargs["sslmode"] == kwargs["gssencmode"] == kwargs["channel_binding"] == "disable"
    assert kwargs["require_auth"] == "none,password,md5,scram-sha-256"
    assert kwargs["connect_timeout"] == 5
    assert kwargs["options"] == "-c statement_timeout=5000 -c lock_timeout=3000"
    statements = connection.execute.call_args_list
    assert statements[0].args == ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED",)
    assert statements[1].args[0].as_string() == 'SET LOCAL search_path TO "synthetic_core", pg_temp'
    assert statements[2].args == (
        "SELECT pg_advisory_xact_lock(%s)",
        (_lock_key("binding", "synthetic_core", key(binding())),),
    )
    connection.__exit__.assert_called_once_with(None, None, None)


def test_explicit_password_sent_only_as_connection_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, _ = mock_connection(monkeypatch)
    database = PostgresDatabase(config(password=SecretStr("synthetic-pass")))
    with database.transaction(binding()):
        pass
    assert connect.call_args.kwargs["password"] == "synthetic-pass"
    assert "synthetic-pass" not in repr(database)


def test_scope_lock_is_stable_and_namespaced() -> None:
    one = _lock_key("binding", "core", key(binding()))
    assert -(2**63) <= one < 2**63
    assert one == _lock_key("binding", "core", key(binding()))
    assert one != _lock_key("binding", "other", key(binding()))
    assert one != _lock_key("binding", "core", key(binding("other")))
    assert one != _lock_key("schema", "core", key(binding()))
    assert key(binding()) == '["synthetic-subject", "synthetic-client", "local-private", "miori"]'


@pytest.mark.parametrize("phase", ["connect", "execute", "commit"])
def test_storage_errors_hide_driver_details(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    connect, connection = mock_connection(monkeypatch)
    error = psycopg.OperationalError("synthetic-private-query-and-password")
    if phase == "connect":
        connect.side_effect = error
    elif phase == "execute":
        connection.execute.side_effect = error
    else:
        connection.__exit__.side_effect = error
    with pytest.raises(CoreError) as failure, PostgresDatabase(config()).transaction(binding()):
        pass
    assert failure.value.code == "storage_unavailable"
    assert "synthetic-private" not in str(failure.value)
    assert failure.value.__suppress_context__


def test_application_error_and_cancellation_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    _, connection = mock_connection(monkeypatch)
    database = PostgresDatabase(config())
    error = CoreError(409, "revision_conflict", "Conversation changed")
    with pytest.raises(CoreError) as failure, database.transaction(binding()):
        raise error
    assert failure.value is error
    assert connection.__exit__.call_args.args[0] is CoreError
    with pytest.raises(asyncio.CancelledError), database.transaction(binding()):
        raise asyncio.CancelledError
    assert connection.__exit__.call_args.args[0] is asyncio.CancelledError
