import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).parent.parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
ENVIRONMENTS_DIR = ROOT_DIR / "environments"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}")
    path.chmod(0o755)


def _write_recording_curl(path: Path, event_log: Path) -> None:
    _write_executable(
        path,
        f'url="${{@: -1}}"\n'
        'if [[ "$url" == *localhost:8000* || "$url" == *127.0.0.1:8000* ]]; then\n'
        '  for _ in {1..100}; do\n'
        f'    grep -Fxq "start-backend.sh" "{event_log}" 2>/dev/null && break\n'
        '    sleep 0.01\n'
        '  done\n'
        f'  grep -Fxq "start-backend.sh" "{event_log}" 2>/dev/null\n'
        'fi\n'
        f'printf "%s\\n" "curl $*" >> "{event_log}"\n',
    )


def _prepare_orchestrator(tmp_path: Path, script_name: str) -> tuple[Path, Path, Path]:
    scripts_dir = tmp_path / "scripts"
    (scripts_dir / "lib").mkdir(parents=True)
    for relative_path in [script_name, "lib/process.sh", "lib/readiness.sh", "lib/profile.sh"]:
        source = SCRIPTS_DIR / relative_path
        target = scripts_dir / relative_path
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o755)
    shutil.copytree(ENVIRONMENTS_DIR, tmp_path / "environments")

    event_log = tmp_path / "events.log"
    for service in [
        "setup-backend.sh",
        "start-ollama.sh",
        "start-voicevox.sh",
        "start-backend.sh",
        "start-frontend.sh",
    ]:
        _write_executable(
            scripts_dir / service,
            f'printf "%s\\n" "{service}" >> "{event_log}"\n',
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "curl", "exit 0\n")
    return scripts_dir / script_name, bin_dir, event_log


def _prepare_frontend_start(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    scripts_dir = tmp_path / "scripts"
    (scripts_dir / "lib").mkdir(parents=True)
    for relative_path in ["start-frontend.sh", "lib/profile.sh"]:
        source = SCRIPTS_DIR / relative_path
        target = scripts_dir / relative_path
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o755)
    shutil.copytree(ENVIRONMENTS_DIR, tmp_path / "environments")
    (tmp_path / "frontend" / "node_modules").mkdir(parents=True)

    event_log = tmp_path / "events.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "npm", f'printf "%s\\n" "npm $*" >> "{event_log}"\n')
    return scripts_dir / "start-frontend.sh", bin_dir, event_log, tmp_path / "resolved.json"


def _prepare_standalone_service(tmp_path: Path, script_name: str) -> tuple[Path, Path, Path]:
    scripts_dir = tmp_path / "scripts"
    (scripts_dir / "lib").mkdir(parents=True)
    for relative_path in [script_name, "lib/profile.sh", "lib/readiness.sh"]:
        source = SCRIPTS_DIR / relative_path
        target = scripts_dir / relative_path
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o755)
    shutil.copytree(ENVIRONMENTS_DIR, tmp_path / "environments")

    event_log = tmp_path / "events.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ["curl", "docker", "ollama"]:
        _write_executable(
            bin_dir / command,
            f'printf "%s\\n" "{command} $*" >> "{event_log}"\n',
        )
    venv_bin = tmp_path / "backend" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    _write_executable(
        venv_bin / "uvicorn",
        f'printf "%s\\n" "uvicorn $*" >> "{event_log}"\n',
    )
    return scripts_dir / script_name, bin_dir, event_log


def _clean_env(bin_dir: Path, **overrides: str) -> dict[str, str]:
    blocked = {
        "DS_PROFILE",
        "DS_PROFILE_REPORT",
        "VOICE_CHAT_E2E_BACKEND",
        "CHAT_E2E_BACKEND",
        "CHAT_E2E_BACKEND_ORIGIN",
        "VOICE_CHAT_E2E_BACKEND_REPORT",
    }
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.update(overrides)
    return env


def _run(script: Path, bin_dir: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script)],
        env=_clean_env(bin_dir, **env_overrides),
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_should_resolve_before_starting_any_development_service(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(tmp_path, "start-all.sh")

    result = _run(script, bin_dir, DS_PROFILE="missing-profile")

    assert result.returncode != 0
    assert not event_log.exists() or event_log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "script_name",
    ["start-backend.sh", "start-ollama.sh", "start-voicevox.sh"],
)
def test_should_reject_mocked_or_disabled_standalone_service_before_start(
    tmp_path: Path,
    script_name: str,
):
    script, bin_dir, event_log = _prepare_standalone_service(tmp_path, script_name)

    result = _run(script, bin_dir, DS_PROFILE="test-mocked")

    assert result.returncode != 0
    assert "startup requires real/managed" in result.stderr
    assert not event_log.exists() or event_log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("caller_script_dir", [None, "/not-the-scripts-directory"])
def test_should_resolve_child_scripts_independently_of_caller_script_dir(
    tmp_path: Path,
    caller_script_dir: str | None,
):
    _, bin_dir, event_log = _prepare_orchestrator(tmp_path, "start-all.sh")
    scripts_dir = tmp_path / "scripts"
    assignment = "unset SCRIPT_DIR" if caller_script_dir is None else f'SCRIPT_DIR="{caller_script_dir}"'
    wrapper = tmp_path / "invoke-profile-stack.sh"
    _write_executable(
        wrapper,
        f'source "{scripts_dir / "lib/process.sh"}"\n'
        f'source "{scripts_dir / "lib/readiness.sh"}"\n'
        f'source "{scripts_dir / "lib/profile.sh"}"\n'
        f'{assignment}\n'
        'profile_resolve "dev"\n'
        'profile_start_stack\n',
    )

    result = _run(wrapper, bin_dir)

    assert result.returncode == 0, result.stderr
    assert event_log.read_text(encoding="utf-8").splitlines() == [
        "setup-backend.sh",
        "start-ollama.sh",
        "start-voicevox.sh",
        "start-backend.sh",
        "start-frontend.sh",
    ]


def test_should_reject_missing_readiness_path_before_setup_or_service_start(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(tmp_path, "start-all.sh")
    profile_path = tmp_path / "environments" / "profiles" / "dev.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["dependencies"]["backend"].pop("readinessPath")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run(script, bin_dir, DS_PROFILE="dev")

    assert result.returncode != 0
    assert "dependencies.backend.readinessPath" in result.stderr
    assert not event_log.exists() or event_log.read_text(encoding="utf-8") == ""


def test_should_use_dev_as_explicit_development_default(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(tmp_path, "start-all.sh")
    report_path = tmp_path / "resolved.json"

    result = _run(script, bin_dir, DS_PROFILE_REPORT=str(report_path))

    assert result.returncode == 0, result.stderr
    assert json.loads(report_path.read_text(encoding="utf-8"))["effectiveProfile"] == "dev"
    assert event_log.read_text(encoding="utf-8").splitlines() == [
        "setup-backend.sh",
        "start-ollama.sh",
        "start-voicevox.sh",
        "start-backend.sh",
        "start-frontend.sh",
    ]


def test_should_start_only_frontend_for_mocked_profile(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(
        tmp_path,
        "start-voice-chat-e2e.sh",
    )
    report_path = tmp_path / "resolved.json"

    result = _run(
        script,
        bin_dir,
        DS_PROFILE="test-mocked",
        DS_PROFILE_REPORT=str(report_path),
    )

    assert result.returncode == 0, result.stderr
    assert event_log.read_text(encoding="utf-8").splitlines() == ["start-frontend.sh"]
    assert json.loads(report_path.read_text(encoding="utf-8"))["dependencies"]["backend"]["mode"] == "mock"


def test_should_reject_external_frontend_before_starting_e2e_services(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(
        tmp_path,
        "start-voice-chat-e2e.sh",
    )
    profile_path = tmp_path / "environments" / "profiles" / "integration-text.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["dependencies"]["frontend"] = {
        "mode": "real",
        "source": "external",
        "baseUrl": "http://frontend.example:5173",
        "readinessPath": "/",
    }
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run(script, bin_dir, DS_PROFILE="integration-text")

    assert result.returncode != 0
    assert "frontend startup requires real/managed" in result.stderr
    assert not event_log.exists() or event_log.read_text(encoding="utf-8") == ""


def test_should_keep_real_voice_service_startup_order(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(
        tmp_path,
        "start-voice-chat-e2e.sh",
    )
    _write_recording_curl(bin_dir / "curl", event_log)

    result = _run(script, bin_dir, DS_PROFILE="integration-voice")

    assert result.returncode == 0, result.stderr
    assert event_log.read_text(encoding="utf-8").splitlines() == [
        "setup-backend.sh",
        "start-ollama.sh",
        "curl -sf http://localhost:11434/api/tags",
        "start-voicevox.sh",
        "start-backend.sh",
        "curl -sf http://localhost:8000/",
        "start-frontend.sh",
    ]


def test_should_check_external_dependency_without_starting_managed_service(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(
        tmp_path,
        "start-voice-chat-e2e.sh",
    )
    profile_path = tmp_path / "environments" / "profiles" / "integration-text.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["dependencies"]["ollama"]["source"] = "external"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _write_recording_curl(bin_dir / "curl", event_log)

    result = _run(script, bin_dir, DS_PROFILE="integration-text")

    assert result.returncode == 0, result.stderr
    events = event_log.read_text(encoding="utf-8").splitlines()
    assert events == [
        "setup-backend.sh",
        "curl -sf http://localhost:11434/api/tags",
        "start-backend.sh",
        "curl -sf http://localhost:8000/",
        "start-frontend.sh",
    ]


def test_should_treat_legacy_backend_origin_override_as_external_backend(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(
        tmp_path,
        "start-voice-chat-e2e.sh",
    )
    override = "http://127.0.0.1:18000"
    report_path = tmp_path / "resolved.json"
    _write_recording_curl(bin_dir / "curl", event_log)

    result = _run(
        script,
        bin_dir,
        DS_PROFILE="integration-text",
        DS_PROFILE_REPORT=str(report_path),
        CHAT_E2E_BACKEND_ORIGIN=override,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["dependencies"]["backend"]["source"] == "external"
    assert event_log.read_text(encoding="utf-8").splitlines() == [
        "start-ollama.sh",
        "curl -sf http://localhost:11434/api/tags",
        f"curl -sf {override}/",
        "start-frontend.sh",
    ]


def test_should_use_integration_voice_as_explicit_e2e_default(tmp_path: Path):
    script, bin_dir, _ = _prepare_orchestrator(tmp_path, "start-voice-chat-e2e.sh")
    report_path = tmp_path / "resolved.json"

    result = _run(script, bin_dir, DS_PROFILE_REPORT=str(report_path))

    assert result.returncode == 0, result.stderr
    assert json.loads(report_path.read_text(encoding="utf-8"))["effectiveProfile"] == "integration-voice"


def test_should_route_legacy_mock_selection_through_resolved_profile(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(
        tmp_path,
        "start-voice-chat-e2e.sh",
    )
    report_path = tmp_path / "resolved.json"

    result = _run(
        script,
        bin_dir,
        VOICE_CHAT_E2E_BACKEND="mock",
        DS_PROFILE_REPORT=str(report_path),
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["effectiveProfile"] == "test-mocked"
    assert report["selectionSource"] == "legacy-environment"
    assert report["compatibility"]["warnings"]
    assert event_log.read_text(encoding="utf-8").splitlines() == ["start-frontend.sh"]


def test_should_reach_frontend_with_separate_report_from_legacy_report_variable(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(tmp_path, "start-voice-chat-e2e.sh")
    legacy_report_path = tmp_path / "voice-chat-backend.json"
    resolved_report_path = tmp_path / "resolved-profile.json"
    frontend_stub = script.parent / "start-frontend.sh"
    _write_executable(
        frontend_stub,
        f'printf "%s\\n" "DS_PROFILE_REPORT=$DS_PROFILE_REPORT" >> "{event_log}"\n',
    )

    result = _run(
        script,
        bin_dir,
        DS_PROFILE="test-mocked",
        VOICE_CHAT_E2E_BACKEND_REPORT=str(legacy_report_path),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(resolved_report_path.read_text(encoding="utf-8"))["effectiveProfile"] == "test-mocked"
    assert json.loads(legacy_report_path.read_text(encoding="utf-8"))["mode"] == "mock"
    assert event_log.read_text(encoding="utf-8").splitlines() == [
        f"DS_PROFILE_REPORT={resolved_report_path}",
    ]


def test_should_export_resolved_environment_and_report_to_frontend(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(
        tmp_path,
        "start-voice-chat-e2e.sh",
    )
    report_path = tmp_path / "resolved.json"
    frontend_stub = script.parent / "start-frontend.sh"
    _write_executable(
        frontend_stub,
        f'printf "%s\\n" "DS_PROFILE_REPORT=$DS_PROFILE_REPORT" >> "{event_log}"\n'
        f'printf "%s\\n" "DS_BACKEND_ORIGIN=$DS_BACKEND_ORIGIN" >> "{event_log}"\n'
        f'printf "%s\\n" "RAG_ENABLED=$RAG_ENABLED" >> "{event_log}"\n',
    )

    result = _run(
        script,
        bin_dir,
        DS_PROFILE="integration-text",
        DS_PROFILE_REPORT=str(report_path),
    )

    assert result.returncode == 0, result.stderr
    assert event_log.read_text(encoding="utf-8").splitlines()[-3:] == [
        f"DS_PROFILE_REPORT={report_path}",
        "DS_BACKEND_ORIGIN=http://localhost:8000",
        "RAG_ENABLED=false",
    ]


def test_should_clear_inactive_dependency_environment_before_starting_frontend(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(
        tmp_path,
        "start-voice-chat-e2e.sh",
    )
    report_path = tmp_path / "resolved.json"
    frontend_stub = script.parent / "start-frontend.sh"
    _write_executable(
        frontend_stub,
        f'printf "%s\\n" "DS_PROFILE_REPORT=$DS_PROFILE_REPORT" >> "{event_log}"\n'
        f'printf "%s\\n" "RAG_ENABLED=$RAG_ENABLED" >> "{event_log}"\n'
        f'printf "%s\\n" "DS_BACKEND_ORIGIN=${{DS_BACKEND_ORIGIN-unset}}" >> "{event_log}"\n'
        f'printf "%s\\n" "OLLAMA_BASE_URL=${{OLLAMA_BASE_URL-unset}}" >> "{event_log}"\n'
        f'printf "%s\\n" "VOICEVOX_BASE_URL=${{VOICEVOX_BASE_URL-unset}}" >> "{event_log}"\n',
    )

    result = _run(
        script,
        bin_dir,
        DS_PROFILE="test-mocked",
        DS_PROFILE_REPORT=str(report_path),
        DS_BACKEND_ORIGIN="http://stale-backend.example",
        OLLAMA_BASE_URL="http://stale-ollama.example",
        VOICEVOX_BASE_URL="http://stale-voicevox.example",
        RAG_ENABLED="true",
    )

    assert result.returncode == 0, result.stderr
    assert event_log.read_text(encoding="utf-8").splitlines() == [
        f"DS_PROFILE_REPORT={report_path}",
        "RAG_ENABLED=false",
        "DS_BACKEND_ORIGIN=unset",
        "OLLAMA_BASE_URL=unset",
        "VOICEVOX_BASE_URL=unset",
    ]


def test_should_reject_disabled_backend_before_startup(tmp_path: Path):
    script, bin_dir, event_log = _prepare_orchestrator(
        tmp_path,
        "start-voice-chat-e2e.sh",
    )
    profile_path = tmp_path / "environments" / "profiles" / "test-mocked.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["dependencies"]["backend"] = {"mode": "disabled", "source": None}
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run(script, bin_dir, DS_PROFILE="test-mocked")

    assert result.returncode != 0
    assert "backend" in result.stderr.lower()
    assert not event_log.exists() or event_log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda report: "{", "invalid JSON"),
        (
            lambda report: json.dumps({**report, "reportSchemaVersion": 2}),
            "reportSchemaVersion",
        ),
        (
            lambda report: json.dumps(
                {
                    **report,
                    "derivedEnvironment": {
                        key: value
                        for key, value in report["derivedEnvironment"].items()
                        if key != "RAG_ENABLED"
                    },
                }
            ),
            "derivedEnvironment",
        ),
    ],
)
def test_should_not_start_frontend_with_invalid_existing_report(
    mutation,
    expected_error: str,
    tmp_path: Path,
):
    script, bin_dir, event_log, report_path = _prepare_frontend_start(tmp_path)
    resolve_result = subprocess.run(
        [
            "python3",
            str(tmp_path / "environments" / "profile.py"),
            "resolve",
            "--report",
            str(report_path),
            "--default-profile",
            "dev",
        ],
        env=_clean_env(bin_dir),
        capture_output=True,
        text=True,
    )
    assert resolve_result.returncode == 0, resolve_result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_path.write_text(mutation(report), encoding="utf-8")

    result = _run(script, bin_dir, DS_PROFILE_REPORT=str(report_path))

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not event_log.exists() or event_log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("profile_name", "frontend_dependency"),
    [
        (
            "dev",
            {
                "mode": "real",
                "source": "external",
                "baseUrl": "http://frontend.example:5173",
                "readinessPath": "/",
            },
        ),
        ("test-mocked", {"mode": "disabled", "source": None}),
    ],
)
def test_should_not_start_standalone_frontend_unless_profile_manages_it(
    tmp_path: Path,
    profile_name: str,
    frontend_dependency: dict[str, object],
):
    script, bin_dir, event_log, report_path = _prepare_frontend_start(tmp_path)
    profile_path = tmp_path / "environments" / "profiles" / f"{profile_name}.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["dependencies"]["frontend"] = frontend_dependency
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run(
        script,
        bin_dir,
        DS_PROFILE=profile_name,
        DS_PROFILE_REPORT=str(report_path),
    )

    assert result.returncode != 0
    assert "frontend startup requires real/managed" in result.stderr
    assert not event_log.exists() or event_log.read_text(encoding="utf-8") == ""
