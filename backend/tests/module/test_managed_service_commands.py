from __future__ import annotations

import json
from pathlib import Path

from tests.environment_test_support import RecordingRunner, resolved_runtime_paths


def _dependency(*, service: str, host: str, port: int, reload: bool | None = None):
    dependency: dict[str, object] = {
        "mode": "real",
        "source": "managed",
        "baseUrl": f"http://{host}:{port}",
        "host": host,
        "port": port,
        "readinessPath": "/",
        "readinessUrl": f"http://{host}:{port}/",
    }
    if service == "backend":
        dependency["reload"] = reload
    return dependency


def _start_responses(container_id: str) -> list[dict[str, object]]:
    return [
        {"returncode": 1, "stdout": "", "stderr": "not found"},
        {"returncode": 0, "stdout": "", "stderr": ""},
        {"returncode": 0, "stdout": "", "stderr": ""},
        {
            "returncode": 0,
            "stdout": json.dumps(
                [{
                    "Id": container_id,
                    "State": {
                        "StartedAt": "2026-08-30T00:00:00Z",
                        "Running": True,
                    },
                }]
            ),
            "stderr": "",
        },
    ]


def test_should_start_dev_backend_with_resolved_endpoint_and_reload(
    tmp_path: Path,
) -> None:
    from adapters.backend import BackendAdapter

    runner = RecordingRunner(_start_responses("a" * 64))
    adapter = BackendAdapter(tmp_path, resolved_runtime_paths(tmp_path), runner)

    result = adapter.start(
        _dependency(service="backend", host="127.0.0.1", port=8000, reload=True),
        {"DS_ENVIRONMENT_ID": "test", "WHISPER_BASE_URL": "http://127.0.0.1:50022"},
    )

    assert result.container_identity == {
        "containerId": "a" * 64,
        "startedAt": "2026-08-30T00:00:00Z",
    }
    assert runner.calls[0] == ("docker", "inspect", "digital-souls-test-backend")
    assert runner.calls[1][-2:] == ("build", "backend")
    assert runner.calls[2][-5:] == (
        "up", "--detach", "--no-deps", "--no-build", "backend"
    )
    environment_path = resolved_runtime_paths(tmp_path).runtime_report_dir / "containers" / "backend.env"
    environment = environment_path.read_text(encoding="utf-8")
    assert 'DS_BACKEND_HOST="127.0.0.1"' in environment
    assert 'DS_BACKEND_PORT="8000"' in environment
    assert 'DS_BACKEND_RELOAD="true"' in environment
    assert 'WHISPER_BASE_URL="http://127.0.0.1:50022"' in environment
    assert environment_path.stat().st_mode & 0o777 == 0o600


def test_should_pull_dogfood_frontend_by_immutable_digest(tmp_path: Path) -> None:
    from adapters.frontend import FrontendAdapter

    image = f"ghcr.io/example/digital-souls-frontend@sha256:{'b' * 64}"
    runner = RecordingRunner(_start_responses("c" * 64))
    adapter = FrontendAdapter(
        tmp_path,
        resolved_runtime_paths(tmp_path),
        runner,
        effective_profile="dogfood",
    )
    profile_report = tmp_path / "resolved-profile.json"
    profile_report.write_text("{}\n", encoding="utf-8")
    config_directory = tmp_path / "config"
    config_directory.mkdir()

    adapter.start(
        _dependency(service="frontend", host="127.0.0.1", port=15173),
        {
            "DS_ENVIRONMENT_ID": "dogfood",
            "DS_BACKEND_ORIGIN": "http://127.0.0.1:18000",
            "DS_RUNTIME_UID": "10001",
            "DS_RUNTIME_GID": "10001",
            "DS_PROFILE_REPORT": str(profile_report),
            "DOGFOOD_CONFIG_DIR": str(config_directory),
            "DOGFOOD_FRONTEND_IMAGE": image,
            "DOGFOOD_BACKUP_AUTHENTICATION_KEY": "must-not-reach-frontend",
        },
    )

    assert runner.calls[1][-2:] == ("pull", "frontend")
    environment_path = config_directory / "containers" / "frontend.env"
    environment = environment_path.read_text(encoding="utf-8")
    assert f'DS_FRONTEND_IMAGE="{image}"' in environment
    assert 'DS_BACKEND_ORIGIN="http://127.0.0.1:18000"' in environment
    assert "DOGFOOD_BACKUP_AUTHENTICATION_KEY" not in environment


def test_should_use_the_resolved_frontend_endpoint_not_base_url(tmp_path: Path) -> None:
    from adapters.frontend import FrontendAdapter

    dependency = _dependency(service="frontend", host="127.0.0.1", port=15173)
    dependency["baseUrl"] = "http://127.0.0.1:5173"
    runner = RecordingRunner(_start_responses("d" * 64))
    profile_report = tmp_path / "resolved-profile.json"
    profile_report.write_text("{}\n", encoding="utf-8")
    FrontendAdapter(tmp_path, resolved_runtime_paths(tmp_path), runner).start(
        dependency,
        {
            "DS_ENVIRONMENT_ID": "test",
            "DS_PROFILE_REPORT": str(profile_report),
        },
    )

    environment_path = resolved_runtime_paths(tmp_path).runtime_report_dir / "containers" / "frontend.env"
    environment = environment_path.read_text(encoding="utf-8")
    assert 'DS_FRONTEND_PORT="15173"' in environment
    assert 'DS_FRONTEND_HOST="127.0.0.1"' in environment
