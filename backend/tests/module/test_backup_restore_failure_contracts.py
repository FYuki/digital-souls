from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.backup_restore_test_support import (
    TEST_AUTHENTICATION_KEY,
    initialized_runtime,
)


def _backup_arguments() -> Namespace:
    return Namespace(
        command="backup",
        environment="test",
        repository_root="/tmp/repository",
        backup_root="/tmp/backups",
        retention_count=3,
    )


def _restore_arguments() -> Namespace:
    return Namespace(
        command="restore",
        environment="test",
        repository_root="/tmp/repository",
        backup_directory="/tmp/backups/backup-generation",
    )


def test_rollback_dual_01_keeps_both_failures_without_rendering_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app import main

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(
        tmp_path, repository_root, environment_id="dogfood", name="runtime"
    )
    migration_secret = "conversation-body-primary-secret"
    compensation_secret = "backup-authentication-key-secondary-secret"
    migration_error = RuntimeError(migration_secret)
    compensation_error = RuntimeError(compensation_secret)
    rollback = main._SchemaRollbackContext(
        tmp_path / "generation", TEST_AUTHENTICATION_KEY
    )
    monkeypatch.setattr(
        main,
        "initialize_conversation_history_schema",
        Mock(side_effect=migration_error),
    )
    monkeypatch.setattr(
        main,
        "restore_backup",
        Mock(side_effect=compensation_error),
    )

    with pytest.raises(RuntimeError) as captured:
        main._initialize_schema_with_rollback(
            database_path=paths.sqlite_path,
            runtime_paths=paths,
            repository_root=repository_root,
            rollback=rollback,
        )

    assert captured.value.primary_error is migration_error
    assert captured.value.compensation_error is compensation_error
    assert captured.value.compensation_stage == "restore"
    rendered = str(captured.value)
    assert migration_secret not in rendered
    assert compensation_secret not in rendered
    output = capsys.readouterr()
    assert migration_secret not in output.out + output.err
    assert compensation_secret not in output.out + output.err
    assert migration_secret not in caplog.text
    assert compensation_secret not in caplog.text


def test_cli_error_01_classifies_registered_subclass_and_uses_public_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import environment_cli
    from app.backup_restore import BackupArtifactError

    class SpecializedArtifactError(BackupArtifactError):
        pass

    safe_message = "バックアップ成果物エラー12"
    secret = "secret-from-arbitrary-exception-body"
    monkeypatch.setattr(
        BackupArtifactError,
        "public_message",
        safe_message,
        raising=False,
    )
    monkeypatch.setattr(
        environment_cli,
        "backup_environment",
        Mock(side_effect=SpecializedArtifactError(secret)),
    )
    monkeypatch.setattr(
        environment_cli,
        "_parser",
        lambda: Mock(parse_args=_backup_arguments),
    )

    exit_code = environment_cli.main()

    captured = capsys.readouterr()
    assert exit_code == 11
    assert safe_message in captured.err
    assert secret not in captured.err
    assert captured.out == ""


def test_cli_error_01_maps_unregistered_backup_error_to_safe_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import environment_cli
    from app.backup_restore.models import BackupError

    class FutureBackupError(BackupError):
        pass

    secret = "秘密値 /private/conversations/miori.db"
    monkeypatch.setattr(
        environment_cli,
        "backup_environment",
        Mock(side_effect=FutureBackupError(secret)),
    )
    monkeypatch.setattr(
        environment_cli,
        "_parser",
        lambda: Mock(parse_args=_backup_arguments),
    )

    exit_code = environment_cli.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "backup" in captured.err.lower()
    assert secret not in captured.err
    assert captured.out == ""


def test_restore_recovery_required_maps_to_exit_code_16_without_rendering_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import app.backup_restore as backup_restore
    import environment_cli

    assert hasattr(backup_restore, "RestoreRecoveryRequiredError"), (
        "CLI-ERROR-01 requires the restore recovery error to be publicly available"
    )
    error_type = backup_restore.RestoreRecoveryRequiredError
    secret = "marker-secret /private/conversations/miori.db conversation-body"
    monkeypatch.setattr(
        environment_cli,
        "restore_environment_backup",
        Mock(side_effect=error_type(secret)),
    )
    monkeypatch.setattr(
        environment_cli,
        "_parser",
        lambda: Mock(parse_args=_restore_arguments),
    )

    exit_code = environment_cli.main()

    captured = capsys.readouterr()
    assert exit_code == 16
    assert captured.err == "ERROR: interrupted restore recovery is required\n"
    assert secret not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("error_type", (RuntimeError, OSError))
def test_cli_error_01_hides_non_backup_error_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[Exception],
) -> None:
    import environment_cli

    secret = "秘密値 /private/conversations/miori.db conversation-body"
    monkeypatch.setattr(
        environment_cli,
        "backup_environment",
        Mock(side_effect=error_type(secret)),
    )
    monkeypatch.setattr(
        environment_cli,
        "_parser",
        lambda: Mock(parse_args=_backup_arguments),
    )

    exit_code = environment_cli.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == "ERROR: environment operation failed\n"
    assert secret not in captured.err
    assert captured.out == ""
