from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.environment_test_support import RecordingRunner, resolved_profile
from adapters.base import Check, OperationContext


def test_should_import_concrete_adapters_with_backend_package_contract():
    from adapters.backend import BackendAdapter
    from adapters.ollama import OllamaAdapter

    assert BackendAdapter.__name__ == "BackendAdapter"
    assert OllamaAdapter.__name__ == "OllamaAdapter"


ROOT_DIR = Path(__file__).parent.parent.parent
OPERATION_CONTEXT = OperationContext(whisper_enabled=False, chroma_enabled=False)


def test_should_import_adapter_contract_without_loading_concrete_adapters():
    code = (
        "import json, sys; import adapters.base; "
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name in {'adapters.backend','adapters.frontend','adapters.ollama','adapters.voicevox'})))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        env={"PYTHONPATH": str(ROOT_DIR / "environments")},
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == []


def test_should_prepare_missing_frontend_dependencies_without_starting_service(tmp_path: Path):
    from adapters.frontend import FrontendAdapter

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    runner = RecordingRunner()
    adapter = FrontendAdapter(root_dir=tmp_path, runner=runner)

    adapter.prepare(
        resolved_profile()["dependencies"]["frontend"], OPERATION_CONTEXT
    )

    assert runner.calls == [("npm", "install", "--prefix", str(frontend))]


def test_should_start_frontend_in_foreground_without_install_command(tmp_path: Path):
    from adapters.frontend import FrontendAdapter

    frontend = tmp_path / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    runner = RecordingRunner()
    adapter = FrontendAdapter(root_dir=tmp_path, runner=runner)

    specification = adapter.start_specification(
        resolved_profile()["dependencies"]["frontend"]
    )

    assert specification.command == (
        "npm",
        "run",
        "dev",
        "--prefix",
        str(frontend),
        "--",
        "--host",
        "localhost",
        "--port",
        "5173",
        "--strictPort",
    )
    assert runner.calls == []


def test_should_keep_backend_setup_in_prepare_and_uvicorn_in_start(tmp_path: Path):
    from adapters.backend import BackendAdapter

    runner = RecordingRunner()
    adapter = BackendAdapter(root_dir=tmp_path, runner=runner)
    dependency = resolved_profile()["dependencies"]["backend"]

    adapter.prepare(dependency, OPERATION_CONTEXT)
    start = adapter.start_specification(dependency)

    assert runner.calls == [(str(tmp_path / "scripts" / "setup-backend.sh"),)]
    assert start.command == (str(tmp_path / "scripts" / "start-backend.sh"),)


def test_should_classify_missing_whisper_cache_as_preparation_required(tmp_path: Path):
    from adapters.backend import BackendAdapter

    result = BackendAdapter(root_dir=tmp_path, runner=RecordingRunner()).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    whisper = next(check for check in result.checks if check.name == "whisper-model-medium")
    assert whisper.classification == "preparation_required"


def test_should_prepare_whisper_model_in_cache_used_by_backend_runtime(tmp_path: Path):
    from adapters.backend import BackendAdapter
    from app.model_settings import WHISPER_MODEL_NAME, whisper_model_cache

    runner = RecordingRunner()
    adapter = BackendAdapter(root_dir=tmp_path, runner=runner)

    adapter.prepare(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert runner.calls[0] == (str(tmp_path / "scripts" / "setup-backend.sh"),)
    assert runner.calls[1][0] == str(tmp_path / "backend" / ".venv" / "bin" / "python")
    assert repr(WHISPER_MODEL_NAME) in runner.calls[1][2]
    assert repr(str(whisper_model_cache(tmp_path))) in runner.calls[1][2]


def test_should_mark_missing_whisper_cache_as_preparable(tmp_path: Path):
    from adapters.backend import BackendAdapter

    result = BackendAdapter(root_dir=tmp_path, runner=RecordingRunner()).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    whisper = next(check for check in result.checks if check.name == "whisper-model-medium")
    assert whisper.can_prepare is True


def test_should_treat_empty_whisper_cache_as_preparation_required(tmp_path: Path):
    from adapters.backend import BackendAdapter
    from app.model_settings import WHISPER_MODEL_CACHE_DIRECTORY, whisper_model_cache

    (whisper_model_cache(tmp_path) / WHISPER_MODEL_CACHE_DIRECTORY).mkdir(parents=True)

    result = BackendAdapter(root_dir=tmp_path, runner=RecordingRunner()).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    whisper = next(check for check in result.checks if check.name == "whisper-model-medium")
    assert whisper.classification == "preparation_required"


def test_should_require_executable_backend_launchers_during_verify(tmp_path: Path):
    from adapters.backend import BackendAdapter

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "setup-backend.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    start = scripts / "start-backend.sh"
    start.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    start.chmod(0o755)
    venv_bin = tmp_path / "backend" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    for executable in ("python", "uvicorn"):
        (venv_bin / executable).write_text("", encoding="utf-8")

    result = BackendAdapter(root_dir=tmp_path, runner=RecordingRunner()).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=False, chroma_enabled=False),
    )

    setup = next(check for check in result.checks if check.name == "backend-setup-launcher")
    start_check = next(check for check in result.checks if check.name == "backend-start-launcher")
    python = next(check for check in result.checks if check.name == "backend-python")
    uvicorn = next(check for check in result.checks if check.name == "backend-uvicorn")
    assert setup.classification == "preparation_required"
    assert setup.can_prepare is False
    assert start_check.classification == "ready"
    assert python.classification == "preparation_required"
    assert uvicorn.classification == "preparation_required"


def test_should_prepare_chroma_directory_only_in_prepare(tmp_path: Path):
    from adapters.backend import BackendAdapter

    chroma_path = tmp_path / "backend" / "app" / "data" / "chroma"
    adapter = BackendAdapter(root_dir=tmp_path, runner=RecordingRunner())

    verify = adapter.verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=False, chroma_enabled=True),
    )
    adapter.prepare(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=False, chroma_enabled=True),
    )
    prepared = adapter.verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=False, chroma_enabled=True),
    )

    missing = next(check for check in verify.checks if check.name == "chroma-storage")
    ready = next(check for check in prepared.checks if check.name == "chroma-storage")
    assert missing.classification == "preparation_required"
    assert missing.can_prepare is True
    assert chroma_path.is_dir()
    assert ready.classification == "ready"


def test_should_classify_chroma_file_collision_as_not_preparable(tmp_path: Path):
    from adapters.backend import BackendAdapter

    chroma_path = tmp_path / "backend" / "app" / "data" / "chroma"
    chroma_path.parent.mkdir(parents=True)
    chroma_path.write_text("not a directory", encoding="utf-8")

    result = BackendAdapter(root_dir=tmp_path, runner=RecordingRunner()).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=False, chroma_enabled=True),
    )

    chroma = next(check for check in result.checks if check.name == "chroma-storage")
    assert chroma.classification == "preparation_required"
    assert chroma.can_prepare is False


def test_should_classify_unreachable_docker_daemon_without_aborting_verify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from adapters.voicevox import VoicevoxAdapter

    monkeypatch.setattr("adapters.voicevox.shutil.which", lambda command: "/usr/bin/docker")
    runner = RecordingRunner(
        [{"returncode": 1, "stdout": "", "stderr": "cannot connect to Docker daemon"}]
    )

    result = VoicevoxAdapter(tmp_path, runner).verify(
        resolved_profile()["dependencies"]["voicevox"], OPERATION_CONTEXT
    )

    assert result.prerequisites_ready is False
    assert result.checks == (
        Check(
            "voicevox-container",
            "preparation_required",
            "failed to inspect VOICEVOX container: cannot connect to Docker daemon",
            False,
        ),
    )


def test_should_classify_unwritable_chroma_directory_as_not_preparable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from adapters.backend import BackendAdapter
    import adapters.backend

    chroma_path = tmp_path / "backend" / "app" / "data" / "chroma"
    chroma_path.mkdir(parents=True)
    real_access = adapters.backend.os.access
    monkeypatch.setattr(
        adapters.backend.os,
        "access",
        lambda path, mode: False if path == chroma_path else real_access(path, mode),
    )

    result = BackendAdapter(root_dir=tmp_path, runner=RecordingRunner()).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=False, chroma_enabled=True),
    )

    chroma = next(check for check in result.checks if check.name == "chroma-storage")
    assert chroma.classification == "preparation_required"
    assert chroma.can_prepare is False


def test_should_require_gemma_model_when_reusing_ready_ollama(tmp_path: Path):
    from adapters.ollama import OllamaPreparationError, verify_required_model

    with pytest.raises(OllamaPreparationError, match="gemma4:e4b"):
        verify_required_model({"models": [{"name": "other:latest"}]})


def test_should_require_gemma_model_after_started_ollama_becomes_http_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from adapters.ollama import OllamaAdapter
    import adapters.ollama

    monkeypatch.setattr(
        adapters.ollama,
        "_fetch_json",
        lambda _url: {"models": [{"name": "other:latest"}]},
    )

    result = OllamaAdapter(tmp_path).validate_readiness(
        resolved_profile()["dependencies"]["ollama"]
    )

    assert result.classification == "preparation"
    assert result.message is not None and "gemma4:e4b" in result.message


def test_should_reuse_running_voicevox_without_ownership(tmp_path: Path):
    from adapters.voicevox import VoicevoxAdapter

    runner = RecordingRunner(
        [
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":true,"StartedAt":"2026-07-17T00:00:00Z"}}]',
                "stderr": "",
            }
        ]
    )

    result = VoicevoxAdapter(root_dir=tmp_path, runner=runner).start(
        resolved_profile()["dependencies"]["voicevox"], {}
    )

    assert result.state == "reused"
    assert result.owned is False
    assert [call[1] for call in runner.calls if call[0] == "docker"] == ["inspect"]


def test_should_own_only_stopped_voicevox_container_started_by_this_run(tmp_path: Path):
    from adapters.voicevox import VoicevoxAdapter

    runner = RecordingRunner(
        [
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":false,"StartedAt":"2026-07-16T00:00:00Z"}}]',
                "stderr": "",
            },
            {"returncode": 0, "stdout": "container-a\n", "stderr": ""},
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":true,"StartedAt":"2026-07-17T00:00:00Z"}}]',
                "stderr": "",
            },
        ]
    )

    result = VoicevoxAdapter(root_dir=tmp_path, runner=runner).start(
        resolved_profile()["dependencies"]["voicevox"], {}
    )

    assert result.state == "started"
    assert result.owned is True
    assert result.container_identity == {
        "containerId": "container-a",
        "startedAt": "2026-07-17T00:00:00Z",
    }
    assert [call[1] for call in runner.calls if call[0] == "docker"] == [
        "inspect",
        "start",
        "inspect",
    ]


@pytest.mark.parametrize(
    "post_start_inspection",
    [
        {
            "returncode": 1,
            "stdout": "",
            "stderr": "Cannot connect to the Docker daemon",
        },
        {
            "returncode": 0,
            "stdout": '[{"Id":"container-a","State":{"Running":false,"StartedAt":"2026-07-17T00:00:00Z"}}]',
            "stderr": "",
        },
    ],
)
def test_should_rollback_voicevox_when_post_start_inspection_fails(
    tmp_path: Path, post_start_inspection: dict[str, object]
):
    from adapters.voicevox import VoicevoxAdapter

    runner = RecordingRunner(
        [
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":false,"StartedAt":"2026-07-16T00:00:00Z"}}]',
                "stderr": "",
            },
            {"returncode": 0, "stdout": "container-a\n", "stderr": ""},
            post_start_inspection,
            *(
                [
                    {
                        "returncode": 0,
                        "stdout": '[{"Id":"container-a","State":{"Running":true,"StartedAt":"2026-07-17T00:00:00Z"}}]',
                        "stderr": "",
                    }
                ]
                if post_start_inspection["returncode"] != 0
                else []
            ),
            {"returncode": 0, "stdout": "container-a\n", "stderr": ""},
        ]
    )

    with pytest.raises(RuntimeError):
        VoicevoxAdapter(root_dir=tmp_path, runner=runner).start(
            resolved_profile()["dependencies"]["voicevox"], {}
        )

    expected_calls = [
        ("docker", "inspect", "voicevox_engine"),
        ("docker", "start", "voicevox_engine"),
        ("docker", "inspect", "voicevox_engine"),
    ]
    if post_start_inspection["returncode"] != 0:
        expected_calls.append(("docker", "inspect", "voicevox_engine"))
    expected_calls.append(("docker", "stop", "container-a"))
    assert runner.calls == expected_calls


def test_should_return_owned_identity_and_cleanup_failure_when_voicevox_rollback_fails(
    tmp_path: Path,
):
    from adapters.base import AdapterOperationError
    from adapters.voicevox import VoicevoxAdapter

    runner = RecordingRunner(
        [
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":false,"StartedAt":"2026-07-16T00:00:00Z"}}]',
                "stderr": "",
            },
            {"returncode": 0, "stdout": "container-a\n", "stderr": ""},
            {"returncode": 1, "stdout": "", "stderr": "inspect failed"},
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":true,"StartedAt":"2026-07-17T00:00:00Z"}}]',
                "stderr": "",
            },
            {"returncode": 1, "stdout": "", "stderr": "stop failed"},
        ]
    )

    with pytest.raises(AdapterOperationError) as error:
        VoicevoxAdapter(root_dir=tmp_path, runner=runner).start(
            resolved_profile()["dependencies"]["voicevox"], {}
        )

    assert str(error.value) == "failed to inspect VOICEVOX container: inspect failed"
    assert error.value.category == "startup"
    assert error.value.ownership is not None
    assert error.value.ownership.container_identity == {
        "containerId": "container-a",
        "startedAt": "2026-07-17T00:00:00Z",
    }
    assert error.value.cleanup_failure is not None
    assert error.value.cleanup_failure.message == "stop failed"


def test_should_preserve_owned_identity_when_voicevox_rollback_raises(tmp_path: Path):
    from adapters.base import AdapterOperationError
    from adapters.voicevox import VoicevoxAdapter

    class RaisingRollbackRunner(RecordingRunner):
        def run(self, command: tuple[str, ...], cwd: Path) -> dict[str, object]:
            if command[:2] == ("docker", "stop"):
                self.calls.append(command)
                raise OSError("docker daemon disconnected")
            return super().run(command, cwd)

    runner = RaisingRollbackRunner(
        [
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":false,"StartedAt":"2026-07-16T00:00:00Z"}}]',
                "stderr": "",
            },
            {"returncode": 0, "stdout": "container-a\n", "stderr": ""},
            {"returncode": 1, "stdout": "", "stderr": "inspect failed"},
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":true,"StartedAt":"2026-07-17T00:00:00Z"}}]',
                "stderr": "",
            },
        ]
    )

    with pytest.raises(AdapterOperationError) as error:
        VoicevoxAdapter(root_dir=tmp_path, runner=runner).start(
            resolved_profile()["dependencies"]["voicevox"], {}
        )

    assert error.value.ownership is not None
    assert error.value.ownership.container_identity == {
        "containerId": "container-a",
        "startedAt": "2026-07-17T00:00:00Z",
    }
    assert error.value.cleanup_failure is not None
    assert error.value.cleanup_failure.message == "docker daemon disconnected"


def test_should_not_report_pre_start_identity_when_current_voicevox_identity_is_unavailable(
    tmp_path: Path,
):
    from adapters.base import AdapterOperationError
    from adapters.voicevox import VoicevoxAdapter

    runner = RecordingRunner(
        [
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":false,"StartedAt":"2026-07-16T00:00:00Z"}}]',
                "stderr": "",
            },
            {"returncode": 0, "stdout": "container-a\n", "stderr": ""},
            {"returncode": 1, "stdout": "", "stderr": "first inspect failed"},
            {"returncode": 1, "stdout": "", "stderr": "identity unavailable"},
        ]
    )

    with pytest.raises(AdapterOperationError) as error:
        VoicevoxAdapter(root_dir=tmp_path, runner=runner).start(
            resolved_profile()["dependencies"]["voicevox"], {}
        )

    assert error.value.ownership is None
    assert error.value.cleanup_failure is not None
    assert error.value.cleanup_failure.message is not None
    assert "identity unavailable" in error.value.cleanup_failure.message
    assert not any(call[1] == "stop" for call in runner.calls)


def test_should_classify_missing_voicevox_container_as_preparation_failure(tmp_path: Path):
    from adapters.voicevox import VoicevoxAdapter, VoicevoxPreparationError

    runner = RecordingRunner(
        [
            {
                "returncode": 1,
                "stdout": "[]",
                "stderr": "Error response from daemon: No such container: voicevox_engine",
            }
        ]
    )

    with pytest.raises(VoicevoxPreparationError, match="docker run -d --name voicevox_engine"):
        VoicevoxAdapter(root_dir=tmp_path, runner=runner).start(
            resolved_profile()["dependencies"]["voicevox"], {}
        )


def test_should_report_voicevox_inspect_failure_without_setup_guidance(tmp_path: Path):
    from adapters.voicevox import VoicevoxAdapter, VoicevoxInspectionError

    runner = RecordingRunner(
        [
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "Cannot connect to the Docker daemon",
            }
        ]
    )

    with pytest.raises(VoicevoxInspectionError, match="Cannot connect") as error:
        VoicevoxAdapter(root_dir=tmp_path, runner=runner).start(
            resolved_profile()["dependencies"]["voicevox"], {}
        )

    assert "docker run -d --name voicevox_engine" not in str(error.value)
    assert [call[1] for call in runner.calls if call[0] == "docker"] == ["inspect"]


def test_should_not_stop_voicevox_when_container_identity_changed(tmp_path: Path):
    from adapters.voicevox import VoicevoxAdapter

    runner = RecordingRunner(
        [
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":true,"StartedAt":"2026-07-17T00:02:00Z"}}]',
                "stderr": "",
            }
        ]
    )
    service = {
        "containerIdentity": {
            "containerId": "container-a",
            "startedAt": "2026-07-17T00:00:00Z",
        }
    }

    result = VoicevoxAdapter(root_dir=tmp_path, runner=runner).stop(service, 0)

    assert result.result == "skipped_identity_mismatch"
    assert [call[1] for call in runner.calls if call[0] == "docker"] == ["inspect"]


def test_should_stop_voicevox_by_immutable_container_id_when_identity_matches(tmp_path: Path):
    from adapters.voicevox import VoicevoxAdapter

    runner = RecordingRunner(
        [
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":true,"StartedAt":"2026-07-17T00:00:00Z"}}]',
                "stderr": "",
            },
            {"returncode": 0, "stdout": "container-a\n", "stderr": ""},
        ]
    )
    service = {
        "containerIdentity": {
            "containerId": "container-a",
            "startedAt": "2026-07-17T00:00:00Z",
        }
    }

    result = VoicevoxAdapter(root_dir=tmp_path, runner=runner).stop(service, 0)

    assert result.result == "stopped"
    assert runner.calls == [
        ("docker", "inspect", "voicevox_engine"),
        ("docker", "stop", "container-a"),
    ]
