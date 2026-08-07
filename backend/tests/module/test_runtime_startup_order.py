import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI


def test_rt_report_01_runtime_log_uses_safe_projection_only(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app import main
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = resolve_runtime_paths(
        {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")},
        repository_root,
    )
    monkeypatch.setenv("SECRET_TOKEN", "secret-value")
    monkeypatch.setenv("CONVERSATION_BODY", "会話本文")
    monkeypatch.setenv("SYSTEM_PROMPT", "prompt-value")

    with caplog.at_level(logging.INFO):
        main.log_runtime_configuration(paths)

    rendered = caplog.text
    assert "test" in rendered
    assert str(paths.sqlite_path) in rendered
    assert str(paths.chroma_path) in rendered
    assert str(paths.runtime_report_dir) in rendered
    assert str(paths.cache_path) in rendered
    assert "secret-value" not in rendered
    assert "会話本文" not in rendered
    assert "prompt-value" not in rendered


@pytest.mark.anyio
async def test_rt_start_01_invalid_identity_prevents_all_store_initialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import main
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = resolve_runtime_paths(
        {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")},
        repository_root,
    )
    initialize_schema = Mock(side_effect=AssertionError("SQLite must stay closed"))
    sqlite_connect = Mock(side_effect=AssertionError("SQLite must stay closed"))
    create_chat_service = Mock(side_effect=AssertionError("Chroma must stay closed"))
    create_audio_service = Mock(side_effect=AssertionError("cache must stay untouched"))
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(
        main,
        "initialize_runtime_data_root",
        Mock(side_effect=ValueError("environment identity mismatch")),
    )
    monkeypatch.setattr(
        main,
        "initialize_conversation_history_schema",
        initialize_schema,
    )
    monkeypatch.setattr(main.sqlite3, "connect", sqlite_connect)
    monkeypatch.setattr(main._chat_runtime, "create_chat_service", create_chat_service)
    monkeypatch.setattr(main, "create_audio_pipeline_service", create_audio_service)

    with pytest.raises(ValueError, match="environment identity mismatch"):
        async with main.lifespan(FastAPI()):
            pytest.fail("invalid runtime must not start")

    initialize_schema.assert_not_called()
    sqlite_connect.assert_not_called()
    create_chat_service.assert_not_called()
    create_audio_service.assert_not_called()


@pytest.mark.anyio
async def test_rt_start_02_derived_symlink_prevents_all_store_initialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import main
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = resolve_runtime_paths(
        {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")},
        repository_root,
    )
    paths.data_root.mkdir()
    paths.identity_marker_path.write_text(
        '{"schemaVersion": 1, "environmentId": "test"}',
        encoding="utf-8",
    )
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    paths.sqlite_path.symlink_to(external / "conversation-history.db")
    initialize_schema = Mock(side_effect=AssertionError("SQLite must stay closed"))
    sqlite_connect = Mock(side_effect=AssertionError("SQLite must stay closed"))
    create_chat_service = Mock(side_effect=AssertionError("Chroma must stay closed"))
    create_audio_service = Mock(side_effect=AssertionError("cache must stay untouched"))
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(main, "initialize_conversation_history_schema", initialize_schema)
    monkeypatch.setattr(main.sqlite3, "connect", sqlite_connect)
    monkeypatch.setattr(main._chat_runtime, "create_chat_service", create_chat_service)
    monkeypatch.setattr(main, "create_audio_pipeline_service", create_audio_service)

    with pytest.raises(ValueError):
        async with main.lifespan(FastAPI()):
            pytest.fail("invalid runtime must not start")

    initialize_schema.assert_not_called()
    sqlite_connect.assert_not_called()
    create_chat_service.assert_not_called()
    create_audio_service.assert_not_called()
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.anyio
async def test_rt_sqlite_01_startup_passes_one_path_to_schema_wal_and_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import main
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = resolve_runtime_paths(
        {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")},
        repository_root,
    )
    seen: dict[str, Path] = {}

    class StopAfterRepository(RuntimeError):
        pass

    class RecordingWal:
        def __init__(self, *, database_path: Path, **_kwargs: object) -> None:
            seen["wal"] = database_path

    def record_schema(database_path: Path) -> None:
        seen["schema"] = database_path

    def record_repository(*, database_path: Path, **_kwargs: object):
        seen["repository"] = database_path
        raise StopAfterRepository

    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(main, "initialize_runtime_data_root", lambda *_args: None)
    monkeypatch.setattr(main, "initialize_conversation_history_schema", record_schema)
    monkeypatch.setattr(main, "ConversationWalCleanup", RecordingWal)
    monkeypatch.setattr(main, "ConversationHistoryRepository", record_repository)

    with pytest.raises(StopAfterRepository):
        async with main.lifespan(FastAPI()):
            pytest.fail("startup is intentionally stopped after path capture")

    assert seen == {
        "schema": paths.sqlite_path,
        "wal": paths.sqlite_path,
        "repository": paths.sqlite_path,
    }
