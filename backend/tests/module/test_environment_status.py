from __future__ import annotations

import pytest

from tests.environment_test_support import orchestrator_identity, resolved_profile


def test_should_render_owned_managed_and_unowned_external_services_distinctly() -> None:
    from commands.status_command import render_environment_status

    report = {
        "effectiveProfile": {
            "dependencies": {
                "frontend": {"mode": "real", "source": "managed"},
                "backend": {"mode": "real", "source": "managed"},
                "ollama": {"mode": "real", "source": "external"},
                "voicevox": {"mode": "real", "source": "external"},
                "whisper": {"mode": "real", "source": "in_process"},
                "chroma": {"mode": "disabled", "source": None},
            }
        },
        "services": {
            "frontend": {"state": "started", "owned": True},
            "backend": {"state": "started", "owned": True},
            "ollama": {"state": "external", "owned": False},
            "voicevox": {"state": "external", "owned": False},
            "whisper": {"state": "in_process", "owned": False},
            "chroma": {"state": "disabled", "owned": False},
        },
    }
    live_states = {
        "frontend": "ready",
        "backend": "ready",
        "ollama": "ready",
        "voicevox": "unavailable",
    }

    lines = render_environment_status(report, live_states)

    assert lines == [
        "frontend source=managed ownership=owned state=ready",
        "backend source=managed ownership=owned state=ready",
        "ollama source=external ownership=unowned state=ready",
        "voicevox source=external ownership=unowned state=unavailable",
        "whisper source=in_process ownership=unowned state=in_process",
        "chroma source=disabled ownership=unowned state=disabled",
    ]


def test_should_expose_status_as_a_read_only_cli_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import environment_cli

    calls: list[tuple[object, str | None]] = []
    monkeypatch.setattr(
        environment_cli,
        "status_environment",
        lambda root, report: calls.append((root, report)) or 0,
    )
    arguments = environment_cli._parser().parse_args(
        ["status", "--run-report", "/tmp/dogfood-run.json"]
    )

    exit_code = environment_cli._dispatch(arguments)

    assert exit_code == 0
    assert calls == [(environment_cli.ROOT_DIR, "/tmp/dogfood-run.json")]


def test_should_read_live_status_without_mutating_the_selected_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import commands.status_command as status_command
    from app.runtime_data_root import initialize_runtime_data_root
    from http_readiness import ReadinessResult
    from process_control import ProcessIdentity
    from run_report import create_initial_report
    from run_report_store import RunReportStore
    from tests.environment_test_support import resolved_runtime_paths

    runtime_paths = resolved_runtime_paths(tmp_path)
    initialize_runtime_data_root(runtime_paths, tmp_path)
    profile = resolved_profile("integration-voice")
    profile["runtime"] = {
        "environmentId": runtime_paths.environment_id,
        "dataRoot": str(runtime_paths.data_root),
        "sqlitePath": str(runtime_paths.sqlite_path),
        "chromaPath": str(runtime_paths.chroma_path),
        "runtimeReportDirectory": str(runtime_paths.runtime_report_dir),
        "cachePath": str(runtime_paths.cache_path),
    }
    report_path = runtime_paths.runtime_report_dir / "status" / "environment-run.json"
    report = create_initial_report(
        run_id="status-run",
        started_at="2026-08-07T00:00:00+00:00",
        resolved_profile_path=report_path.with_name("resolved-profile.json"),
        effective_profile=profile,
        orchestrator_identity=orchestrator_identity(),
        runtime=profile["runtime"],
    )
    RunReportStore(report_path).save(report)
    before = report_path.read_bytes()
    monkeypatch.setenv("DS_ENVIRONMENT_ID", runtime_paths.environment_id)
    monkeypatch.setenv("DS_DATA_DIR", str(runtime_paths.data_root))
    monkeypatch.setattr(
        status_command,
        "probe_http",
        lambda url, timeout_seconds: ReadinessResult(url, 1, 0.0, "ready"),
    )
    observed_identities: list[ProcessIdentity] = []
    monkeypatch.setattr(
        status_command,
        "process_identity_matches",
        lambda identity: observed_identities.append(identity) or False,
        raising=False,
    )

    exit_code = status_command.status_environment(tmp_path, str(report_path))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "frontend source=managed ownership=unowned state=ready" in output
    assert "orchestrator state=dead" in output
    assert observed_identities == [
        ProcessIdentity.from_report(orchestrator_identity())
    ]
    assert report_path.read_bytes() == before
