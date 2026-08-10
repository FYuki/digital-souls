from __future__ import annotations

import os
import sqlite3
from argparse import Namespace
from pathlib import Path
from time import perf_counter
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

from tests.backup_restore_test_support import (
    TEST_AUTHENTICATION_KEY,
    create_version_two_database,
    initialized_runtime,
)


SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _files_snapshot(directory: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _sqlite_snapshot(database: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for suffix in ("", *SQLITE_SIDECAR_SUFFIXES):
        path = database.with_name(database.name + suffix)
        snapshot[suffix] = path.read_bytes() if path.exists() else None
    return snapshot


@pytest.mark.parametrize(
    ("command", "arguments", "handler_name", "expected"),
    (
        (
            "backup",
            [
                "--environment",
                "test",
                "--repository-root",
                "/tmp/repository",
                "--backup-root",
                "/tmp/backups",
                "--retention-count",
                "3",
            ],
            "backup_environment",
            ("test", "/tmp/repository", "/tmp/backups", 3),
        ),
        (
            "backup-verify",
            ["--backup-directory", "/tmp/backups/backup-one"],
            "verify_environment_backup",
            ("/tmp/backups/backup-one",),
        ),
        (
            "restore",
            [
                "--environment",
                "test",
                "--repository-root",
                "/tmp/repository",
                "--backup-directory",
                "/tmp/backups/backup-one",
            ],
            "restore_environment_backup",
            ("test", "/tmp/repository", "/tmp/backups/backup-one"),
        ),
        (
            "restore-verify",
            [
                "--environment",
                "test",
                "--repository-root",
                "/tmp/repository",
                "--backup-directory",
                "/tmp/backups/backup-one",
            ],
            "verify_restored_environment_backup",
            ("test", "/tmp/repository", "/tmp/backups/backup-one"),
        ),
    ),
)
def test_cli_ops_01_exposes_noninteractive_commands_and_explicit_paths(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    arguments: list[str],
    handler_name: str,
    expected: tuple[object, ...],
) -> None:
    import environment_cli

    calls: list[tuple[object, ...]] = []

    def record_call(*values: object) -> int:
        calls.append(values)
        return 0

    monkeypatch.setattr(
        environment_cli,
        handler_name,
        record_call,
    )
    parsed = environment_cli._parser().parse_args([command, *arguments])

    exit_code = environment_cli._dispatch(parsed)

    assert exit_code == 0
    assert calls == [expected]


def test_cli_ops_01_returns_distinct_exit_codes_for_rejection_classes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import environment_cli
    from app import backup_restore

    monkeypatch.setattr(
        environment_cli,
        "_parser",
        lambda: Mock(
            parse_args=lambda: Namespace(
                command="backup",
                environment="test",
                repository_root="/tmp/repository",
                backup_root="/tmp/backups",
                retention_count=3,
            )
        ),
    )

    exit_codes = []
    for error_name in (
        "BackupIdentityError",
        "BackupArtifactError",
        "BackupSchemaError",
        "RestoreSafetyError",
        "BackupPublicationUncertainError",
        "RestoreDurabilityUncertainError",
    ):
        error_type = getattr(backup_restore, error_name)
        monkeypatch.setattr(
            environment_cli,
            "backup_environment",
            Mock(side_effect=error_type("safe classification")),
        )
        exit_codes.append(environment_cli.main())

    assert all(exit_code != 0 for exit_code in exit_codes)
    assert exit_codes == [10, 11, 12, 13, 14, 15]
    captured = capsys.readouterr()
    assert "safe classification" not in captured.err
    assert captured.out == ""


def test_cli_ops_01_uses_runtime_path_resolver_once_for_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from commands import backup_restore_command

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    backup_root = tmp_path / "backups"
    resolved_paths = object()
    resolver = Mock(return_value=resolved_paths)
    create = Mock(return_value=backup_root / "generation")
    monkeypatch.setattr(backup_restore_command, "resolve_runtime_paths", resolver)
    monkeypatch.setattr(backup_restore_command, "create_backup", create)
    monkeypatch.setenv("DOGFOOD_BACKUP_AUTHENTICATION_KEY", "ab" * 32)

    exit_code = backup_restore_command.backup_environment(
        "test",
        str(repository_root),
        str(backup_root),
        3,
    )

    assert exit_code == 0
    resolver.assert_called_once()
    resolver_environment, resolver_root = resolver.call_args.args
    assert resolver_environment["DS_ENVIRONMENT_ID"] == "test"
    assert resolver_environment["DS_DATA_DIR"] == os.environ["DS_DATA_DIR"]
    assert resolver_root == repository_root
    assert create.call_args.kwargs["runtime_paths"] is resolved_paths
    assert create.call_args.kwargs["backup_root"] == backup_root
    assert create.call_args.kwargs["authentication_key"].value == bytes.fromhex(
        "ab" * 32
    )


@pytest.mark.anyio
async def test_schema_gate_01_backup_failure_prevents_schema_initialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app import main
    from app.runtime_data_root import initialize_runtime_data_root
    from app.runtime_paths import resolve_runtime_paths

    repository_root = Path(__file__).resolve().parents[3]
    paths = resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": "dogfood",
            "DS_DATA_DIR": str(tmp_path / "dogfood-data"),
        },
        repository_root,
    )
    initialize_runtime_data_root(paths, repository_root)
    initialize_schema = Mock(side_effect=AssertionError("migration must not start"))
    backup_gate = Mock(side_effect=RuntimeError("pre-migration backup failed"))
    monkeypatch.setattr(main, "resolve_model_settings", lambda *_args: object())
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(main, "initialize_runtime_data_root", lambda *_args: None)
    monkeypatch.setattr(main, "ensure_schema_backup_gate", backup_gate)
    monkeypatch.setattr(main, "initialize_conversation_history_schema", initialize_schema)

    with pytest.raises(RuntimeError, match="pre-migration backup failed"):
        async with main.lifespan(FastAPI()):
            pytest.fail("startup must stop at the backup gate")

    backup_gate.assert_called_once_with(paths, repository_root)
    initialize_schema.assert_not_called()


@pytest.mark.anyio
async def test_sqlite_lease_01_stops_startup_before_sqlite_open_during_maintenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app import main
    from app.conversation_history.sqlite_lease import (
        SQLiteLeaseUnavailableError,
        acquire_maintenance_lease,
    )

    repository_root = Path(__file__).resolve().parents[3]
    paths = initialized_runtime(tmp_path, repository_root, name="runtime")
    inspect_schema = Mock(side_effect=AssertionError("SQLite must not be opened"))
    monkeypatch.setattr(main, "resolve_model_settings", lambda *_args: object())
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(main, "initialize_runtime_data_root", lambda *_args: None)
    monkeypatch.setattr(main, "inspect_conversation_history_schema", inspect_schema)

    with acquire_maintenance_lease(paths.sqlite_path):
        with pytest.raises(SQLiteLeaseUnavailableError):
            async with main.lifespan(FastAPI()):
                pytest.fail("startup must stop before SQLite is opened")

    inspect_schema.assert_not_called()


@pytest.mark.anyio
async def test_sqlite_lease_01_lifespan_rejects_restore_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app import main
    from app.backup_restore import RestoreSafetyError, create_backup, restore_backup
    from app.conversation_history.schema import initialize_conversation_history_schema
    from app.conversation_history.sqlite_lease import acquire_maintenance_lease

    repository_root = Path(__file__).resolve().parents[3]
    paths = initialized_runtime(tmp_path, repository_root, name="runtime")
    initialize_conversation_history_schema(paths.sqlite_path)
    backup_root = tmp_path / "backups"
    generation = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=backup_root,
        retention_count=2,
        authentication_key=TEST_AUTHENTICATION_KEY,
        git_commit="0123456789abcdef0123456789abcdef01234567",
    )
    audio_service = Mock()
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(main, "initialize_runtime_data_root", lambda *_args: None)
    monkeypatch.setattr(main._chat_runtime, "create_chat_service", Mock())
    monkeypatch.setattr(
        main, "create_audio_pipeline_service", Mock(return_value=audio_service)
    )

    async with main.lifespan(FastAPI()):
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            paths.sqlite_path.with_name(paths.sqlite_path.name + suffix).write_bytes(
                f"sidecar-{suffix}".encode()
            )
        sqlite_before = _sqlite_snapshot(paths.sqlite_path)
        generation_before = _files_snapshot(generation)
        started_at = perf_counter()

        with pytest.raises(RestoreSafetyError):
            restore_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

        assert perf_counter() - started_at < 1
        assert _sqlite_snapshot(paths.sqlite_path) == sqlite_before
        assert _files_snapshot(generation) == generation_before

    with acquire_maintenance_lease(paths.sqlite_path):
        pass


def test_should_create_then_verify_backup_for_dogfood_version_two_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app import main
    from app.backup_restore import create_backup, verify_backup

    repository_root = Path(__file__).resolve().parents[3]
    paths = initialized_runtime(
        tmp_path, repository_root, environment_id="dogfood", name="dogfood-data"
    )
    create_version_two_database(paths.sqlite_path)
    backup_root = tmp_path / "backups"
    operations = Mock()
    create_spy = Mock(wraps=create_backup)
    verify_spy = Mock(wraps=verify_backup)
    operations.attach_mock(create_spy, "create")
    operations.attach_mock(verify_spy, "verify")

    monkeypatch.setenv("DOGFOOD_BACKUP_DIR", str(backup_root))
    monkeypatch.setenv("DOGFOOD_BACKUP_RETENTION_COUNT", "2")
    monkeypatch.setenv("DOGFOOD_BACKUP_AUTHENTICATION_KEY", "ab" * 32)
    monkeypatch.setattr(main, "create_backup", create_spy)
    monkeypatch.setattr(main, "verify_backup", verify_spy)

    rollback = main.ensure_schema_backup_gate(paths, repository_root)

    generations = tuple(backup_root.glob("backup-*"))
    assert [call[0] for call in operations.mock_calls] == ["create", "verify"]
    assert len(generations) == 1
    assert rollback is not None
    assert rollback.generation == generations[0]
    assert rollback.authentication_key == TEST_AUTHENTICATION_KEY
    assert verify_backup(
        backup_directory=generations[0],
        authentication_key=TEST_AUTHENTICATION_KEY,
    ).schema_version == 2


@pytest.mark.parametrize(
    ("environment_id", "database_state"),
    (
        ("dogfood", "current"),
        ("dogfood", "missing"),
        ("test", "version-two"),
    ),
    ids=("current-schema", "missing-database", "non-dogfood"),
)
def test_should_skip_schema_backup_when_migration_gate_does_not_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment_id: str,
    database_state: str,
) -> None:
    from app import main
    from app.conversation_history.schema import initialize_conversation_history_schema

    repository_root = Path(__file__).resolve().parents[3]
    paths = initialized_runtime(
        tmp_path, repository_root, environment_id=environment_id, name="runtime"
    )
    if database_state == "current":
        initialize_conversation_history_schema(paths.sqlite_path)
    elif database_state == "version-two":
        create_version_two_database(paths.sqlite_path)
    create = Mock()
    verify = Mock()
    monkeypatch.setattr(main, "create_backup", create)
    monkeypatch.setattr(main, "verify_backup", verify)

    rollback = main.ensure_schema_backup_gate(paths, repository_root)

    assert rollback is None
    create.assert_not_called()
    verify.assert_not_called()


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("DOGFOOD_BACKUP_DIR", None),
        ("DOGFOOD_BACKUP_DIR", ""),
        ("DOGFOOD_BACKUP_RETENTION_COUNT", None),
        ("DOGFOOD_BACKUP_RETENTION_COUNT", ""),
        ("DOGFOOD_BACKUP_RETENTION_COUNT", "not-a-number"),
        ("DOGFOOD_BACKUP_RETENTION_COUNT", "0"),
        ("DOGFOOD_BACKUP_AUTHENTICATION_KEY", None),
        ("DOGFOOD_BACKUP_AUTHENTICATION_KEY", ""),
        ("DOGFOOD_BACKUP_AUTHENTICATION_KEY", "xyz"),
    ),
    ids=(
        "missing-root",
        "empty-root",
        "missing-retention",
        "empty-retention",
        "nonnumeric-retention",
        "nonpositive-retention",
        "missing-authentication",
        "empty-authentication",
        "invalid-authentication",
    ),
)
def test_should_reject_invalid_schema_backup_configuration_before_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    key: str,
    value: str | None,
) -> None:
    from app import main

    repository_root = Path(__file__).resolve().parents[3]
    paths = initialized_runtime(
        tmp_path, repository_root, environment_id="dogfood", name="runtime"
    )
    create_version_two_database(paths.sqlite_path)
    monkeypatch.setenv("DOGFOOD_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("DOGFOOD_BACKUP_RETENTION_COUNT", "2")
    monkeypatch.setenv("DOGFOOD_BACKUP_AUTHENTICATION_KEY", "ab" * 32)
    if value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, value)
    create = Mock()
    monkeypatch.setattr(main, "create_backup", create)

    with pytest.raises((RuntimeError, ValueError)):
        main.ensure_schema_backup_gate(paths, repository_root)

    create.assert_not_called()


@pytest.mark.anyio
async def test_should_initialize_schema_only_after_real_gate_backup_and_verify(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app import main

    repository_root = Path(__file__).resolve().parents[3]
    paths = initialized_runtime(
        tmp_path, repository_root, environment_id="dogfood", name="runtime"
    )
    create_version_two_database(paths.sqlite_path)
    from app.conversation_history.schema import initialize_conversation_history_schema

    events: list[str] = []

    def record_create(**_kwargs: object) -> Path:
        events.append("create")
        return tmp_path

    create = Mock(side_effect=record_create)
    verify = Mock(side_effect=lambda **_kwargs: events.append("verify"))
    restore = Mock()
    restore_verify = Mock()

    def initialize(database_path: Path) -> None:
        events.append("initialize")
        initialize_conversation_history_schema(database_path)

    initializer = Mock(side_effect=initialize)
    monkeypatch.setenv("DOGFOOD_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("DOGFOOD_BACKUP_RETENTION_COUNT", "2")
    monkeypatch.setenv("DOGFOOD_BACKUP_AUTHENTICATION_KEY", "ab" * 32)
    monkeypatch.setattr(main, "resolve_model_settings", lambda *_args: object())
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(main, "initialize_runtime_data_root", lambda *_args: None)
    monkeypatch.setattr(main, "create_backup", create)
    monkeypatch.setattr(main, "verify_backup", verify)
    monkeypatch.setattr(main, "restore_backup", restore)
    monkeypatch.setattr(main, "verify_restored_backup", restore_verify)
    monkeypatch.setattr(main, "initialize_conversation_history_schema", initializer)
    monkeypatch.setattr(
        main,
        "ConversationWalCleanup",
        Mock(side_effect=RuntimeError("post-initialization startup reached")),
    )

    with pytest.raises(RuntimeError, match="post-initialization startup reached"):
        async with main.lifespan(FastAPI()):
            pytest.fail("startup should continue after schema initialization")

    assert events == ["create", "verify", "initialize"]
    restore.assert_not_called()
    restore_verify.assert_not_called()


@pytest.mark.anyio
async def test_should_restore_verified_generation_when_schema_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app import main
    from app.backup_restore import (
        create_backup,
        restore_backup,
        verify_backup,
        verify_restored_backup,
    )
    from app.conversation_history.schema import initialize_conversation_history_schema

    repository_root = Path(__file__).resolve().parents[3]
    paths = initialized_runtime(
        tmp_path, repository_root, environment_id="dogfood", name="runtime"
    )
    create_version_two_database(paths.sqlite_path)
    operations = Mock()
    create_spy = Mock(wraps=create_backup)
    verify_spy = Mock(wraps=verify_backup)
    restore_spy = Mock(wraps=restore_backup)
    restore_verify_spy = Mock(wraps=verify_restored_backup)
    operations.attach_mock(create_spy, "create")
    operations.attach_mock(verify_spy, "verify")
    operations.attach_mock(restore_spy, "restore")
    operations.attach_mock(restore_verify_spy, "restore_verify")

    def initialize_then_fail(database_path: Path) -> None:
        operations.initialize(database_path)
        initialize_conversation_history_schema(database_path)
        raise RuntimeError("schema initialization failed")

    monkeypatch.setenv("DOGFOOD_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("DOGFOOD_BACKUP_RETENTION_COUNT", "2")
    monkeypatch.setenv("DOGFOOD_BACKUP_AUTHENTICATION_KEY", "ab" * 32)
    monkeypatch.setattr(main, "resolve_model_settings", lambda *_args: object())
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(main, "initialize_runtime_data_root", lambda *_args: None)
    monkeypatch.setattr(main, "create_backup", create_spy)
    monkeypatch.setattr(main, "verify_backup", verify_spy)
    monkeypatch.setattr(main, "restore_backup", restore_spy)
    monkeypatch.setattr(main, "verify_restored_backup", restore_verify_spy)
    monkeypatch.setattr(
        main, "initialize_conversation_history_schema", initialize_then_fail
    )

    with pytest.raises(RuntimeError, match="schema initialization failed"):
        async with main.lifespan(FastAPI()):
            pytest.fail("startup must stop after rollback")

    assert [call[0] for call in operations.mock_calls] == [
        "create",
        "verify",
        "initialize",
        "restore",
        "restore_verify",
    ]
    generation = verify_spy.call_args.kwargs["backup_directory"]
    assert restore_spy.call_args.kwargs["backup_directory"] == generation
    assert restore_verify_spy.call_args.kwargs["backup_directory"] == generation
    assert restore_spy.call_args.kwargs["maintenance_lease"] is not None
    assert (
        restore_spy.call_args.kwargs["authentication_key"]
        == restore_verify_spy.call_args.kwargs["authentication_key"]
        == TEST_AUTHENTICATION_KEY
    )
    with sqlite3.connect(paths.sqlite_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        conversation_count = connection.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()[0]
        assert conversation_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("failure_stage", ("restore", "verification"))
async def test_should_stop_startup_when_schema_rollback_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    from app import main
    from app.backup_restore import RestoreDurabilityUncertainError, restore_backup
    from app.conversation_history.schema import initialize_conversation_history_schema

    repository_root = Path(__file__).resolve().parents[3]
    paths = initialized_runtime(
        tmp_path, repository_root, environment_id="dogfood", name="runtime"
    )
    create_version_two_database(paths.sqlite_path)

    migration_error = RuntimeError("schema initialization failed")
    compensation_error = (
        RestoreDurabilityUncertainError("restore durability is uncertain")
        if failure_stage == "restore"
        else RuntimeError("verification failed")
    )

    def initialize_then_fail(database_path: Path) -> None:
        initialize_conversation_history_schema(database_path)
        raise migration_error

    restore = Mock(wraps=restore_backup)
    restore_verify = Mock()
    if failure_stage == "restore":
        restore.side_effect = compensation_error
    else:
        restore_verify.side_effect = compensation_error
    monkeypatch.setenv("DOGFOOD_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("DOGFOOD_BACKUP_RETENTION_COUNT", "2")
    monkeypatch.setenv("DOGFOOD_BACKUP_AUTHENTICATION_KEY", "ab" * 32)
    monkeypatch.setattr(main, "resolve_model_settings", lambda *_args: object())
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(main, "initialize_runtime_data_root", lambda *_args: None)
    monkeypatch.setattr(
        main, "initialize_conversation_history_schema", initialize_then_fail
    )
    monkeypatch.setattr(main, "restore_backup", restore)
    monkeypatch.setattr(main, "verify_restored_backup", restore_verify)

    with pytest.raises(RuntimeError) as captured:
        async with main.lifespan(FastAPI()):
            pytest.fail("startup must stop when rollback fails")

    assert captured.value.primary_error is migration_error
    assert captured.value.compensation_error is compensation_error
    assert captured.value.compensation_stage == failure_stage
    restore.assert_called_once()
    if failure_stage == "restore":
        restore_verify.assert_not_called()
    else:
        restore_verify.assert_called_once()


@pytest.mark.anyio
@pytest.mark.parametrize("failure_stage", ("create", "verify"))
async def test_should_stop_before_schema_initializer_when_real_gate_operation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    from app import main

    repository_root = Path(__file__).resolve().parents[3]
    paths = initialized_runtime(
        tmp_path, repository_root, environment_id="dogfood", name="runtime"
    )
    create_version_two_database(paths.sqlite_path)
    initializer = Mock(side_effect=AssertionError("initializer must not run"))
    create = Mock(return_value=tmp_path / "generation")
    verify = Mock()
    if failure_stage == "create":
        create.side_effect = RuntimeError("create failed")
    else:
        verify.side_effect = RuntimeError("verify failed")
    monkeypatch.setenv("DOGFOOD_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("DOGFOOD_BACKUP_RETENTION_COUNT", "2")
    monkeypatch.setenv("DOGFOOD_BACKUP_AUTHENTICATION_KEY", "ab" * 32)
    monkeypatch.setattr(main, "resolve_model_settings", lambda *_args: object())
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(main, "initialize_runtime_data_root", lambda *_args: None)
    monkeypatch.setattr(main, "create_backup", create)
    monkeypatch.setattr(main, "verify_backup", verify)
    monkeypatch.setattr(main, "initialize_conversation_history_schema", initializer)

    with pytest.raises(RuntimeError, match=f"{failure_stage} failed"):
        async with main.lifespan(FastAPI()):
            pytest.fail("startup must stop at the backup gate")

    initializer.assert_not_called()
    if failure_stage == "create":
        verify.assert_not_called()


def test_bkp_priv_01_cli_does_not_render_sensitive_exception_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import environment_cli
    from app.backup_restore import BackupArtifactError

    conversation = "本文sentinel"
    secret = "sk-cli-secret"
    monkeypatch.setattr(
        environment_cli,
        "backup_environment",
        Mock(side_effect=BackupArtifactError(f"invalid {conversation} {secret}")),
    )
    arguments = Namespace(
        command="backup",
        environment="test",
        repository_root="/tmp/repository",
        backup_root="/tmp/backups",
        retention_count=3,
    )
    monkeypatch.setattr(
        environment_cli,
        "_parser",
        lambda: Mock(parse_args=lambda: arguments),
    )

    assert environment_cli.main() != 0
    rendered = capsys.readouterr().err
    assert "artifact" in rendered.lower()
    assert conversation not in rendered
    assert secret not in rendered
