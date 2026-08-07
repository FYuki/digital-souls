from __future__ import annotations

import json
from pathlib import Path


def _runtime_paths(tmp_path: Path):
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    return resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": "test",
            "DS_DATA_DIR": str(tmp_path / "runtime-data"),
        },
        repository_root,
    )


def _runtime_projection(data_root: Path) -> dict[str, str]:
    return {
        "environmentId": "test",
        "dataRoot": str(data_root),
        "sqlitePath": str(data_root / "conversation-history.db"),
        "chromaPath": str(data_root / "chroma"),
        "runtimeReportDirectory": str(data_root / "runtime"),
        "cachePath": str(data_root / "cache"),
    }


def test_rt_report_01_safe_projection_contains_paths_but_not_runtime_inputs(
    tmp_path: Path,
) -> None:
    from app.runtime_paths import runtime_paths_projection

    paths = _runtime_paths(tmp_path)
    projection = runtime_paths_projection(paths)
    serialized = json.dumps(projection, ensure_ascii=False)

    assert projection == _runtime_projection(paths.data_root)
    assert "secret-value" not in serialized
    assert "会話本文" not in serialized
    assert "prompt" not in serialized.lower()


def test_rt_report_01_resolved_profile_adds_only_safe_runtime_projection(
    tmp_path: Path,
) -> None:
    from profile_resolution import resolve_profile

    paths = _runtime_paths(tmp_path)
    environment = {
        "DS_PROFILE": "integration-text",
        "DS_ENVIRONMENT_ID": "test",
        "DS_DATA_DIR": str(paths.data_root),
        "SECRET_TOKEN": "secret-value",
        "CONVERSATION_BODY": "会話本文",
        "SYSTEM_PROMPT": "prompt-value",
    }

    report = resolve_profile(environment, None, paths)
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["derivedEnvironment"]["DS_ENVIRONMENT_ID"] == "test"
    assert report["derivedEnvironment"]["DS_DATA_DIR"] == str(paths.data_root)
    assert report["runtime"] == _runtime_projection(paths.data_root)
    assert "secret-value" not in serialized
    assert "会話本文" not in serialized
    assert "prompt-value" not in serialized


def test_rt_report_01_run_report_roundtrip_preserves_runtime_projection(
    tmp_path: Path,
) -> None:
    from run_report import create_initial_report
    from run_report_store import RunReportStore
    from tests.environment_test_support import orchestrator_identity, resolved_profile

    data_root = tmp_path / "runtime-data"
    runtime_report_dir = data_root / "runtime"
    projection = _runtime_projection(data_root)
    report = create_initial_report(
        run_id="runtime-roundtrip",
        started_at="2026-08-07T00:00:00+09:00",
        resolved_profile_path=runtime_report_dir / "resolved-profile.json",
        effective_profile=resolved_profile(),
        orchestrator_identity=orchestrator_identity(),
        runtime=projection,
    )
    store = RunReportStore(runtime_report_dir / "environment-run.json")

    store.save(report)
    restored = store.load()

    assert restored["runtime"] == projection


def test_rt_report_01_pending_run_report_contains_runtime_projection(
    tmp_path: Path,
) -> None:
    from run_report import create_pending_report
    from tests.environment_test_support import orchestrator_identity

    data_root = tmp_path / "runtime-data"
    runtime_report_dir = data_root / "runtime"
    projection = _runtime_projection(data_root)

    report = create_pending_report(
        run_id="runtime-pending",
        started_at="2026-08-07T00:00:00+09:00",
        resolved_profile_path=runtime_report_dir / "resolved-profile.json",
        orchestrator_identity=orchestrator_identity(),
        runtime=projection,
    )

    assert report["runtime"] == projection
