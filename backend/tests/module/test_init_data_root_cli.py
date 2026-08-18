from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.backup_restore_test_support import (
    CONVERSATION_SENTINEL,
    FIXED_BACKUP_TIME,
    FIXED_COMMIT,
    TEST_AUTHENTICATION_KEY,
    create_history_database,
    database_projection,
    initialized_runtime,
)


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> tuple[int, str, str]:
    import environment_cli

    monkeypatch.setattr(sys, "argv", ["environment_cli.py", *arguments])
    exit_code = environment_cli.main()
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _init_arguments(environment_id: str, repository_root: Path) -> list[str]:
    return [
        "init-data-root",
        "--environment",
        environment_id,
        "--repository-root",
        str(repository_root),
    ]


@pytest.mark.parametrize("environment_id", ("dev", "test", "dogfood"))
def test_should_initialize_data_root_when_initialization_is_repeated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    environment_id: str,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    data_root = tmp_path / f"{environment_id}-data"
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))

    first = _run_cli(
        monkeypatch, capsys, _init_arguments(environment_id, repository_root)
    )
    second = _run_cli(
        monkeypatch, capsys, _init_arguments(environment_id, repository_root)
    )

    expected_output = {
        "status": "ok",
        "environmentId": environment_id,
        "dataRoot": str(data_root),
    }
    assert first[0] == 0
    assert second[0] == 0
    assert json.loads(first[1]) == expected_output
    assert json.loads(second[1]) == expected_output
    assert first[2] + second[2] == ""
    assert json.loads(
        (data_root / ".environment-identity.json").read_text(encoding="utf-8")
    ) == {"schemaVersion": 1, "environmentId": environment_id}


@pytest.mark.parametrize(
    ("arguments", "missing_option"),
    (
        (
            ["init-data-root", "--repository-root", "/tmp/repository"],
            "--environment",
        ),
        (["init-data-root", "--environment", "test"], "--repository-root"),
    ),
)
def test_should_report_missing_required_option_when_init_argument_is_omitted(
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    missing_option: str,
) -> None:
    import environment_cli

    with pytest.raises(SystemExit) as captured:
        environment_cli._parser().parse_args(arguments)

    assert captured.value.code == 2
    assert missing_option in capsys.readouterr().err


def test_should_reject_data_root_option_when_public_arguments_are_parsed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import environment_cli

    valid_arguments = [
        "init-data-root",
        "--environment",
        "test",
        "--repository-root",
        "/tmp/repository",
    ]
    parsed = environment_cli._parser().parse_args(valid_arguments)

    with pytest.raises(SystemExit) as captured:
        environment_cli._parser().parse_args(
            [*valid_arguments, "--data-root", "/tmp/data"]
        )

    assert vars(parsed) == {
        "command": "init-data-root",
        "environment": "test",
        "repository_root": "/tmp/repository",
    }
    assert captured.value.code == 2
    assert "unrecognized arguments: --data-root /tmp/data" in capsys.readouterr().err


def test_should_delegate_once_when_data_root_is_resolved_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from commands import backup_restore_command

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    data_root = tmp_path / "選択-data"
    resolved_paths = Mock(environment_id="test", data_root=data_root)
    resolver = Mock(return_value=resolved_paths)
    initializer = Mock()
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))
    monkeypatch.setattr(backup_restore_command, "resolve_runtime_paths", resolver)
    monkeypatch.setattr(
        backup_restore_command, "initialize_runtime_data_root", initializer
    )

    exit_code = backup_restore_command.initialize_environment_data_root(
        "test", str(repository_root)
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    resolver.assert_called_once_with(
        {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(data_root)},
        repository_root,
    )
    initializer.assert_called_once_with(resolved_paths, repository_root)
    assert captured.out == (
        '{"status":"ok","environmentId":"test",'
        f'"dataRoot":"{data_root}"}}\n'
    )
    assert captured.err == ""


def test_should_reject_without_traceback_when_marker_environment_differs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / ".environment-identity.json").write_text(
        '{"schemaVersion": 1, "environmentId": "dev"}\n', encoding="utf-8"
    )
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))

    result = _run_cli(monkeypatch, capsys, _init_arguments("test", repository_root))

    assert result == (
        1,
        "",
        "ERROR: runtime data root environment identity does not match\n",
    )


def test_should_reject_without_traceback_when_data_root_is_symlink(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    actual_root = tmp_path / "actual"
    actual_root.mkdir()
    data_root = tmp_path / "linked-data"
    data_root.symlink_to(actual_root, target_is_directory=True)
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))

    result = _run_cli(monkeypatch, capsys, _init_arguments("test", repository_root))

    assert result == (1, "", "ERROR: DS_DATA_DIR must not contain symlinks\n")
    assert list(actual_root.iterdir()) == []


def test_should_reject_without_traceback_when_derived_path_is_symlink(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    data_root = tmp_path / "data"
    external = tmp_path / "external"
    data_root.mkdir()
    external.mkdir()
    (data_root / "chroma").symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))

    result = _run_cli(monkeypatch, capsys, _init_arguments("test", repository_root))

    assert result == (
        1,
        "",
        "ERROR: runtime derived path must not contain symlinks\n",
    )
    assert list(external.iterdir()) == []


def test_should_reject_without_traceback_when_dogfood_root_is_in_repository(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    data_root = repository_root / "dogfood-data"
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))

    result = _run_cli(
        monkeypatch, capsys, _init_arguments("dogfood", repository_root)
    )

    assert result == (
        1,
        "",
        "ERROR: dogfood data root must be outside the repository\n",
    )
    assert not data_root.exists()


def test_should_hide_content_when_unmarked_root_contains_persistent_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    data_root = tmp_path / "data"
    data_root.mkdir()
    database = data_root / "conversation-history.db"
    database.write_text(CONVERSATION_SENTINEL, encoding="utf-8")
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))

    result = _run_cli(monkeypatch, capsys, _init_arguments("test", repository_root))

    assert result == (
        1,
        "",
        "ERROR: runtime data root identity marker is missing\n",
    )
    assert database.read_text(encoding="utf-8") == CONVERSATION_SENTINEL
    assert CONVERSATION_SENTINEL not in result[1] + result[2]


def test_should_hide_details_and_traceback_when_unexpected_error_occurs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import environment_cli

    secret = "sk-private-key and private conversation body"
    monkeypatch.setattr(
        environment_cli,
        "initialize_environment_data_root",
        Mock(side_effect=RuntimeError(secret)),
    )

    result = _run_cli(
        monkeypatch,
        capsys,
        _init_arguments("test", Path("/tmp/repository")),
    )

    assert result == (1, "", "ERROR: environment operation failed\n")
    assert secret not in result[1] + result[2]


def test_should_fail_safely_when_data_root_environment_variable_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    authentication_key = TEST_AUTHENTICATION_KEY.value.hex()
    monkeypatch.delenv("DS_DATA_DIR", raising=False)
    monkeypatch.setenv("DOGFOOD_BACKUP_AUTHENTICATION_KEY", authentication_key)
    monkeypatch.setenv("CONVERSATION_CANARY", CONVERSATION_SENTINEL)

    result = _run_cli(
        monkeypatch,
        capsys,
        _init_arguments("test", Path("/tmp/repository")),
    )

    assert result == (1, "", "ERROR: environment operation failed\n")
    cli_output = result[1] + result[2]
    assert "Traceback" not in cli_output
    assert authentication_key not in cli_output
    assert CONVERSATION_SENTINEL not in cli_output


def test_should_restore_and_verify_when_root_is_initialized_through_public_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from app.backup_restore import create_backup
    from app.conversation_history.schema import SCHEMA_VERSION

    repository_root = Path(__file__).resolve().parents[3]
    source_paths = initialized_runtime(tmp_path, repository_root, name="source")
    source_connection = create_history_database(source_paths, wal=False)
    try:
        generation = create_backup(
            runtime_paths=source_paths,
            repository_root=repository_root,
            backup_root=tmp_path / "backups",
            retention_count=1,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )
    finally:
        source_connection.close()
    destination = tmp_path / "restore-drill"
    monkeypatch.setenv("DS_DATA_DIR", str(destination))
    monkeypatch.setenv(
        "DOGFOOD_BACKUP_AUTHENTICATION_KEY", TEST_AUTHENTICATION_KEY.value.hex()
    )

    init_result = _run_cli(
        monkeypatch, capsys, _init_arguments("test", repository_root)
    )
    restore_result = _run_cli(
        monkeypatch,
        capsys,
        [
            "restore",
            "--environment",
            "test",
            "--repository-root",
            str(repository_root),
            "--backup-directory",
            str(generation),
        ],
    )
    verify_result = _run_cli(
        monkeypatch,
        capsys,
        [
            "restore-verify",
            "--environment",
            "test",
            "--repository-root",
            str(repository_root),
            "--backup-directory",
            str(generation),
        ],
    )

    assert init_result[0] == 0
    assert restore_result[0] == 0
    assert verify_result[0] == 0
    assert json.loads(restore_result[1]) == {
        "status": "ok",
        "artifacts": [
            {
                "filename": "conversation-history.db",
                "schemaVersion": SCHEMA_VERSION,
                "recordCount": 1,
            },
            {
                "filename": "persona-memory.db",
                "schemaVersion": 1,
                "recordCount": 0,
            },
        ],
    }
    assert json.loads(verify_result[1]) == {
        "status": "ok",
        "artifacts": [
            {
                "filename": "conversation-history.db",
                "schemaVersion": SCHEMA_VERSION,
                "recordCount": 1,
            },
            {
                "filename": "persona-memory.db",
                "schemaVersion": 1,
                "recordCount": 0,
            },
        ],
    }
    assert init_result[2] + restore_result[2] + verify_result[2] == ""
    cli_output = "".join((*init_result[1:], *restore_result[1:], *verify_result[1:]))
    assert CONVERSATION_SENTINEL not in cli_output
    assert TEST_AUTHENTICATION_KEY.value.hex() not in cli_output
    assert database_projection(destination / "conversation-history.db") == (
        SCHEMA_VERSION,
        1,
        CONVERSATION_SENTINEL,
    )
