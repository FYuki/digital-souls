from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _runtime_paths(tmp_path: Path, environment_id: str = "test"):
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir(exist_ok=True)
    return resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": environment_id,
            "DS_DATA_DIR": str(tmp_path / "runtime-data"),
        },
        repository_root,
    )


def test_rt_sqlite_01_conversation_config_uses_resolved_sqlite_path(
    tmp_path: Path,
) -> None:
    from app.conversation_history.config import resolve_conversation_history_config

    paths = _runtime_paths(tmp_path)

    config = resolve_conversation_history_config(paths)

    assert config.database_path == paths.sqlite_path


def test_rt_chroma_01_add_and_query_use_the_explicit_same_chroma_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.unit.test_memory_chroma_store import FakePersistentClient

    fake_chromadb = ModuleType("chromadb")
    setattr(fake_chromadb, "PersistentClient", FakePersistentClient)
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    sys.modules.pop("app.memory.chroma_store", None)
    chroma_store = importlib.import_module("app.memory.chroma_store")
    FakePersistentClient.instances.clear()
    paths = _runtime_paths(tmp_path)

    chroma_store.add_memory(
        "miori",
        "00000000-0000-4000-8000-000000000052",
        [0.1, 0.2],
        "記憶本文",
        {"role": "user", "timestamp": "2026-08-07T00:00:00+00:00"},
        chroma_path=paths.chroma_path,
    )
    chroma_store.query_memories(
        "miori", [0.1, 0.2], 3, chroma_path=paths.chroma_path
    )

    assert [client.path for client in FakePersistentClient.instances] == [
        str(paths.chroma_path),
        str(paths.chroma_path),
    ]


def test_rt_chroma_01_requires_a_resolved_path_instead_of_module_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_memory_chroma_store import FakePersistentClient

    fake_chromadb = ModuleType("chromadb")
    setattr(fake_chromadb, "PersistentClient", FakePersistentClient)
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    from app.memory import chroma_store

    with pytest.raises(TypeError):
        chroma_store.query_memories("miori", [0.1], 1)


def test_rt_cache_01_audio_runtime_uses_resolved_whisper_cache(
    tmp_path: Path,
) -> None:
    from app.audio_pipeline import resolve_audio_runtime_config
    from app.model_settings import resolve_model_settings

    paths = _runtime_paths(tmp_path)

    config = resolve_audio_runtime_config(resolve_model_settings({}), paths)

    assert config.whisper_download_root == str(paths.whisper_cache_path)


def test_rt_cache_01_backend_adapter_uses_the_same_resolved_cache(
    tmp_path: Path,
) -> None:
    from adapters.backend import BackendAdapter
    from adapters.base import OperationContext

    from tests.environment_test_support import RecordingRunner, resolved_profile

    paths = _runtime_paths(tmp_path)
    runner = RecordingRunner()
    adapter = BackendAdapter(
        root_dir=tmp_path / "repository",
        runtime_paths=paths,
        runner=runner,
    )

    adapter.prepare(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert runner.calls[1][4] == str(paths.whisper_cache_path)
    assert runner.calls[2][4] == str(paths.whisper_cache_path)


def test_rt_clean_01_backend_prepare_rejects_dogfood_marker_before_side_effects(
    tmp_path: Path,
) -> None:
    from adapters.backend import BackendAdapter
    from adapters.base import OperationContext

    from tests.environment_test_support import RecordingRunner, resolved_profile

    paths = _runtime_paths(tmp_path)
    paths.data_root.mkdir(exist_ok=True)
    paths.identity_marker_path.write_text(
        json.dumps({"schemaVersion": 1, "environmentId": "dogfood"}),
        encoding="utf-8",
    )
    sentinel = paths.data_root / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    runner = RecordingRunner()
    adapter = BackendAdapter(
        root_dir=tmp_path / "repository",
        runtime_paths=paths,
        runner=runner,
    )

    with pytest.raises(ValueError, match="environment identity"):
        adapter.prepare(
            resolved_profile()["dependencies"]["backend"],
            OperationContext(whisper_enabled=True, chroma_enabled=True),
        )

    assert runner.calls == []
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert json.loads(paths.identity_marker_path.read_text(encoding="utf-8")) == {
        "schemaVersion": 1,
        "environmentId": "dogfood",
    }


def test_rt_clean_02_backend_prepare_rejects_cache_symlink_before_side_effects(
    tmp_path: Path,
) -> None:
    from adapters.backend import BackendAdapter
    from adapters.base import OperationContext

    from tests.environment_test_support import RecordingRunner, resolved_profile

    paths = _runtime_paths(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    paths.cache_path.symlink_to(external, target_is_directory=True)
    runner = RecordingRunner()
    adapter = BackendAdapter(
        root_dir=tmp_path / "repository",
        runtime_paths=paths,
        runner=runner,
    )

    with pytest.raises(ValueError):
        adapter.prepare(
            resolved_profile()["dependencies"]["backend"],
            OperationContext(whisper_enabled=True, chroma_enabled=True),
        )

    assert runner.calls == []
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_rt_cli_01_rejects_report_override_outside_runtime_directory_before_write(
    tmp_path: Path,
) -> None:
    from environment_options import resolve_output_paths
    from profile_types import ProfileError

    paths = _runtime_paths(tmp_path)
    outside = tmp_path / "outside" / "environment-run.json"

    with pytest.raises(ProfileError, match="runtime"):
        resolve_output_paths(
            run_report_argument=str(outside),
            profile_report_argument=None,
            environment={},
            run_id="run-52",
            runtime_paths=paths,
        )

    assert not outside.parent.exists()


def test_rt_cli_01_keeps_report_override_inside_runtime_directory(
    tmp_path: Path,
) -> None:
    from environment_options import resolve_output_paths

    paths = _runtime_paths(tmp_path)
    report_path = paths.runtime_report_dir / "custom" / "environment-run.json"

    output = resolve_output_paths(
        run_report_argument=str(report_path),
        profile_report_argument=None,
        environment={},
        run_id="run-52",
        runtime_paths=paths,
    )

    assert output.run_report == report_path
    assert output.profile_report == report_path.parent / "resolved-profile.json"


def test_rt_cli_02_rejects_default_reports_through_runtime_symlink(
    tmp_path: Path,
) -> None:
    from environment_options import resolve_output_paths
    from profile_types import ProfileError

    paths = _runtime_paths(tmp_path)
    paths.data_root.mkdir(exist_ok=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    paths.runtime_report_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(ProfileError):
        resolve_output_paths(
            run_report_argument=None,
            profile_report_argument=None,
            environment={},
            run_id="run-52",
            runtime_paths=paths,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(external.iterdir()) == [sentinel]


def test_rt_cli_02_rejects_standalone_profile_reports_through_runtime_symlink(
    tmp_path: Path,
) -> None:
    from profile_report_store import resolve_report_paths
    from profile_types import ProfileError

    paths = _runtime_paths(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    paths.runtime_report_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(ProfileError):
        resolve_report_paths(None, {}, None, paths)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(external.iterdir()) == [sentinel]


def test_rt_clean_01_down_rejects_dogfood_marker_before_stop_or_report_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import commands.down_command as down_command
    from run_report import create_initial_report

    from app.runtime_paths import runtime_paths_projection
    from tests.environment_test_support import orchestrator_identity, resolved_profile

    paths = _runtime_paths(tmp_path)
    paths.data_root.mkdir(exist_ok=True)
    marker = {"schemaVersion": 1, "environmentId": "dogfood"}
    paths.identity_marker_path.write_text(json.dumps(marker), encoding="utf-8")
    sentinel = paths.data_root / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    report = create_initial_report(
        run_id="guarded-down",
        started_at="2026-08-07T00:00:00+09:00",
        resolved_profile_path=paths.runtime_report_dir / "resolved-profile.json",
        effective_profile=resolved_profile(),
        orchestrator_identity=orchestrator_identity(),
        runtime=runtime_paths_projection(paths),
    )

    class GuardedStore:
        def __init__(self, _path: Path) -> None:
            pass

        def load(self) -> dict[str, object]:
            return report

        def update(self, _transform):
            raise AssertionError("report update must not be reached")

    monkeypatch.setenv("DS_ENVIRONMENT_ID", "test")
    monkeypatch.setenv("DS_DATA_DIR", str(paths.data_root))
    monkeypatch.setattr(down_command, "RunReportStore", GuardedStore)
    monkeypatch.setattr(
        down_command,
        "request_process_stop",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("process stop must not be reached")
        ),
    )
    monkeypatch.setattr(
        down_command,
        "cleanup_environment_services",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cleanup must not be reached")
        ),
    )

    with pytest.raises(ValueError, match="environment identity"):
        down_command.down_environment(
            tmp_path / "repository",
            str(paths.runtime_report_dir / "environment-run.json"),
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert json.loads(paths.identity_marker_path.read_text(encoding="utf-8")) == marker
