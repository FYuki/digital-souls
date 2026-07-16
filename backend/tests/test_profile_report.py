import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).parent.parent.parent
ENVIRONMENTS_DIR = ROOT_DIR / "environments"
DEPENDENCY_NAMES = {"frontend", "backend", "ollama", "voicevox", "whisper", "chroma"}


def _copy_environments(tmp_path: Path) -> Path:
    target = tmp_path / "environments"
    shutil.copytree(ENVIRONMENTS_DIR, target)
    return target


def _clean_env(**overrides: str) -> dict[str, str]:
    blocked = {
        "DS_PROFILE",
        "DS_PROFILE_REPORT",
        "VOICE_CHAT_E2E_BACKEND",
        "CHAT_E2E_BACKEND",
        "CHAT_E2E_BACKEND_ORIGIN",
        "VOICE_CHAT_E2E_BACKEND_REPORT",
    }
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    env.update(overrides)
    return env


def _run(
    environments_dir: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(environments_dir / "profile.py"), *arguments],
        env=_clean_env() if env is None else env,
        capture_output=True,
        text=True,
    )


def _resolve(
    environments_dir: Path,
    report_path: Path,
    profile: str,
    **extra_env: str,
) -> subprocess.CompletedProcess[str]:
    return _run(
        environments_dir,
        "resolve",
        "--report",
        str(report_path),
        env=_clean_env(DS_PROFILE=profile, **extra_env),
    )


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("profile", "capabilities"),
    [
        ("test-mocked", ["mocked-e2e"]),
        ("integration-text", ["text-chat-real"]),
        ("integration-voice", ["text-chat-real", "voice-chat-real"]),
    ],
)
def test_should_derive_capabilities_from_dependencies(
    profile: str,
    capabilities: list[str],
    tmp_path: Path,
):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(environments_dir, report_path, profile)

    assert result.returncode == 0, result.stderr
    assert _read_json(report_path)["capabilities"] == capabilities


def test_should_derive_rag_capability_for_real_in_process_chroma(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    profile_path = environments_dir / "profiles" / "integration-text.json"
    profile = _read_json(profile_path)
    profile["dependencies"]["chroma"] = {"mode": "real", "source": "in_process"}
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    report_path = tmp_path / "resolved.json"

    result = _resolve(environments_dir, report_path, "integration-text")

    assert result.returncode == 0, result.stderr
    assert _read_json(report_path)["capabilities"] == ["text-chat-real", "rag-real"]


def test_should_write_complete_v1_report_with_timezone_timestamp(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(environments_dir, report_path, "integration-voice")

    assert result.returncode == 0, result.stderr
    report = _read_json(report_path)
    assert report["reportSchemaVersion"] == 1
    assert report["requestedProfile"] == "integration-voice"
    assert report["effectiveProfile"] == "integration-voice"
    assert report["profile"]["schemaVersion"] == 1
    assert report["profile"]["name"] == "integration-voice"
    assert set(report["dependencies"]) == DEPENDENCY_NAMES
    assert datetime.fromisoformat(report["generatedAt"]).tzinfo is not None


def test_should_derive_readiness_urls_from_base_urls_and_paths(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(environments_dir, report_path, "integration-voice")

    assert result.returncode == 0, result.stderr
    dependencies = _read_json(report_path)["dependencies"]
    assert dependencies["frontend"]["readinessUrl"] == "http://localhost:5173/"
    assert dependencies["backend"]["readinessUrl"] == "http://localhost:8000/"
    assert dependencies["ollama"]["readinessUrl"] == "http://localhost:11434/api/tags"
    assert dependencies["voicevox"]["readinessUrl"] == "http://localhost:50021/version"
    assert "readinessUrl" not in dependencies["whisper"]
    assert "readinessUrl" not in dependencies["chroma"]


def test_should_append_validated_readiness_path_without_query_or_fragment(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    profile_path = environments_dir / "profiles" / "integration-text.json"
    profile = _read_json(profile_path)
    backend = profile["dependencies"]["backend"]
    backend["source"] = "external"
    backend["baseUrl"] = "https://backend.example/api"
    backend["readinessPath"] = "/health/%20"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    report_path = tmp_path / "resolved.json"

    result = _resolve(environments_dir, report_path, "integration-text")

    assert result.returncode == 0, result.stderr
    readiness_url = _read_json(report_path)["dependencies"]["backend"]["readinessUrl"]
    assert readiness_url == "https://backend.example/api/health/%20"
    assert "?" not in readiness_url
    assert "#" not in readiness_url


@pytest.mark.parametrize(
    "readiness_path",
    ["//health", "/health?token=secret", "/health#fragment", "/health\nnext", "/health/%zz"],
)
def test_should_reject_unsafe_readiness_path_without_replacing_report(
    readiness_path: str,
    tmp_path: Path,
):
    environments_dir = _copy_environments(tmp_path)
    profile_path = environments_dir / "profiles" / "integration-text.json"
    profile = _read_json(profile_path)
    backend = profile["dependencies"]["backend"]
    backend["source"] = "external"
    backend["readinessPath"] = readiness_path
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    report_path = tmp_path / "resolved.json"
    original = b'{"knownGood":true}\n'
    report_path.write_bytes(original)

    result = _resolve(environments_dir, report_path, "integration-text")

    assert result.returncode != 0
    assert "dependencies.backend.readinessPath" in result.stderr
    assert readiness_path not in result.stderr
    assert report_path.read_bytes() == original


def test_should_apply_backend_origin_override_to_report_and_derived_environment(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"
    override = "http://127.0.0.1:18000"

    result = _resolve(
        environments_dir,
        report_path,
        "integration-text",
        CHAT_E2E_BACKEND_ORIGIN=override,
    )

    assert result.returncode == 0, result.stderr
    report = _read_json(report_path)
    assert report["dependencies"]["backend"]["source"] == "external"
    assert report["dependencies"]["backend"]["baseUrl"] == override
    assert report["dependencies"]["backend"]["readinessUrl"] == f"{override}/"
    assert report["derivedEnvironment"]["DS_BACKEND_ORIGIN"] == override


def test_should_reject_backend_origin_override_for_mock_backend(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(
        environments_dir,
        report_path,
        "test-mocked",
        CHAT_E2E_BACKEND_ORIGIN="http://127.0.0.1:18000",
    )

    assert result.returncode != 0
    assert "CHAT_E2E_BACKEND_ORIGIN" in result.stderr
    assert not report_path.exists()


@pytest.mark.parametrize(
    "override",
    [
        "http://user:password@127.0.0.1:18000",
        "http://127.0.0.1:18000?token=secret",
        "http://127.0.0.1:18000#secret",
        "http://127.0.0.1:18000/api",
    ],
)
def test_should_reject_secret_bearing_or_non_origin_backend_override_without_replacing_report(
    override: str,
    tmp_path: Path,
):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"
    original = b'{"knownGood":true}\n'
    report_path.write_bytes(original)

    result = _resolve(
        environments_dir,
        report_path,
        "integration-text",
        CHAT_E2E_BACKEND_ORIGIN=override,
    )

    assert result.returncode != 0
    assert "CHAT_E2E_BACKEND_ORIGIN" in result.stderr
    assert override not in result.stderr
    assert report_path.read_bytes() == original


def test_should_allowlist_derived_environment_and_exclude_process_secrets(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"
    secret = "must-not-be-copied"
    env = _clean_env(DS_PROFILE="integration-voice", API_SECRET=secret, UNRELATED_VALUE=secret)

    result = _run(
        environments_dir,
        "resolve",
        "--report",
        str(report_path),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    report = _read_json(report_path)
    assert set(report["derivedEnvironment"]) == {
        "OLLAMA_BASE_URL",
        "VOICEVOX_BASE_URL",
        "RAG_ENABLED",
        "DS_BACKEND_ORIGIN",
    }
    serialized = json.dumps(report)
    assert secret not in serialized
    assert "API_SECRET" not in serialized
    assert "UNRELATED_VALUE" not in serialized


@pytest.mark.parametrize(
    ("profile", "expected_environment"),
    [
        ("test-mocked", {"RAG_ENABLED": "false"}),
        (
            "integration-text",
            {
                "OLLAMA_BASE_URL": "http://localhost:11434",
                "RAG_ENABLED": "false",
                "DS_BACKEND_ORIGIN": "http://localhost:8000",
            },
        ),
    ],
)
def test_should_emit_only_environment_values_used_by_selected_profile(
    profile: str,
    expected_environment: dict[str, str],
    tmp_path: Path,
):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(environments_dir, report_path, profile)

    assert result.returncode == 0, result.stderr
    assert _read_json(report_path)["derivedEnvironment"] == expected_environment


def test_should_replace_existing_report_atomically(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"
    report_path.write_text('{"old": true}', encoding="utf-8")
    old_inode = report_path.stat().st_ino

    result = _resolve(environments_dir, report_path, "test-mocked")

    assert result.returncode == 0, result.stderr
    assert report_path.stat().st_ino != old_inode
    assert _read_json(report_path)["effectiveProfile"] == "test-mocked"
    assert list(tmp_path.glob(f".{report_path.name}.*")) == []


def test_should_preserve_existing_report_when_resolution_fails(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"
    original = b'{"knownGood":true}\n'
    report_path.write_bytes(original)

    result = _resolve(
        environments_dir,
        report_path,
        "integration-voice",
        CHAT_E2E_BACKEND="mock",
    )

    assert result.returncode != 0
    assert report_path.read_bytes() == original


def test_should_preserve_resolved_report_when_legacy_report_cannot_be_written(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"
    legacy_report_path = tmp_path / "legacy-report"
    original = b'{"knownGood":true}\n'
    report_path.write_bytes(original)
    legacy_report_path.mkdir()
    env = _clean_env(
        DS_PROFILE="integration-voice",
        VOICE_CHAT_E2E_BACKEND_REPORT=str(legacy_report_path),
    )

    result = _run(environments_dir, "resolve", "--report", str(report_path), env=env)

    assert result.returncode != 0
    assert report_path.read_bytes() == original
    assert legacy_report_path.is_dir()
    assert list(tmp_path.glob(".*.backup.*")) == []


def test_should_restore_legacy_report_when_resolved_report_cannot_be_committed(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved-report"
    legacy_report_path = tmp_path / "legacy.json"
    original = b'{"mode":"mock","reasons":["known good"]}\n'
    report_path.mkdir()
    legacy_report_path.write_bytes(original)
    env = _clean_env(
        DS_PROFILE="integration-voice",
        VOICE_CHAT_E2E_BACKEND_REPORT=str(legacy_report_path),
    )

    result = _run(environments_dir, "resolve", "--report", str(report_path), env=env)

    assert result.returncode != 0
    assert report_path.is_dir()
    assert legacy_report_path.read_bytes() == original
    assert list(tmp_path.glob(".*.backup.*")) == []


def test_should_write_legacy_backend_report_next_to_resolved_report(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"
    legacy_report_path = tmp_path / "voice-chat-backend.json"

    result = _resolve(environments_dir, report_path, "integration-voice")

    assert result.returncode == 0, result.stderr
    assert _read_json(legacy_report_path) == {
        "mode": "real",
        "reasons": ["resolved from DS_PROFILE=integration-voice"],
    }


def test_should_separate_resolved_report_from_legacy_report_path(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    legacy_report_path = tmp_path / "voice-chat-backend.json"
    resolved_report_path = tmp_path / "resolved-profile.json"
    env = _clean_env(
        DS_PROFILE="test-mocked",
        VOICE_CHAT_E2E_BACKEND_REPORT=str(legacy_report_path),
    )

    result = _run(environments_dir, "resolve", env=env)

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == resolved_report_path
    assert _read_json(resolved_report_path)["effectiveProfile"] == "test-mocked"
    assert _read_json(legacy_report_path) == {
        "mode": "mock",
        "reasons": ["resolved from DS_PROFILE=test-mocked"],
    }


def test_should_preserve_reports_when_resolved_and_legacy_paths_collide(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    shared_report_path = tmp_path / "shared.json"
    aliased_report_path = tmp_path / "reports" / ".." / shared_report_path.name
    original = b'{"knownGood":true}\n'
    shared_report_path.write_bytes(original)
    env = _clean_env(
        DS_PROFILE="integration-voice",
        VOICE_CHAT_E2E_BACKEND_REPORT=str(shared_report_path),
    )

    result = _run(
        environments_dir,
        "resolve",
        "--report",
        str(aliased_report_path),
        env=env,
    )

    assert result.returncode != 0
    assert str(shared_report_path) in result.stderr
    assert shared_report_path.read_bytes() == original
