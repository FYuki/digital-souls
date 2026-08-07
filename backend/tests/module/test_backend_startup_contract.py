from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.environment_entrypoint_test_support import write_executable


ROOT_DIR = Path(__file__).parent.parent.parent.parent


def _start_command(start: Path) -> list[str]:
    return [str(start), "--host", "localhost", "--port", "8000", "--reload"]


def _dogfood_start_command(start: Path) -> list[str]:
    return [str(start), "--host", "localhost", "--port", "18000"]


def _profile_environment(profile_name: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DS_PROFILE", "DS_PROFILE_REPORT"}
    }
    return {**environment, "DS_PROFILE": profile_name}


def _copy_backend_scripts(tmp_path: Path) -> tuple[Path, Path, Path]:
    scripts = tmp_path / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(ROOT_DIR / "scripts" / "lib" / "profile.sh", scripts / "lib" / "profile.sh")
    shutil.copytree(ROOT_DIR / "environments", tmp_path / "environments")
    backend = tmp_path / "backend"
    backend_app = backend / "app"
    backend_app.mkdir(parents=True)
    (backend_app / "__init__.py").write_text("", encoding="utf-8")
    for name in (
        "environment.py",
        "model_settings.py",
        "runtime_paths.py",
        "runtime_data_root.py",
    ):
        shutil.copy2(ROOT_DIR / "backend" / "app" / name, backend_app / name)
    for name in ("setup-backend.sh", "start-backend.sh"):
        shutil.copy2(ROOT_DIR / "scripts" / name, scripts / name)
    return scripts / "setup-backend.sh", scripts / "start-backend.sh", backend


def test_should_prepare_backend_before_start_without_starting_uvicorn(tmp_path: Path):
    setup, _start, backend = _copy_backend_scripts(tmp_path)
    (backend / "requirements.txt").write_text("# runtime\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    event_log = tmp_path / "events.log"
    write_executable(
        bin_dir / "python3",
        f'printf "%s\\n" "python $*" >> "{event_log}"\n'
        'venv_dir="${@: -1}"\nmkdir -p "$venv_dir/bin"\n'
        f"cat > \"$venv_dir/bin/pip\" <<'SH'\n"
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "pip $*" >> "{event_log}"\n'
        "SH\n"
        'chmod +x "$venv_dir/bin/pip"\n',
    )
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = subprocess.run([str(setup)], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    events = event_log.read_text(encoding="utf-8").splitlines()
    backend_argument = backend.parent / "scripts" / ".." / "backend"
    assert events == [
        f"python -m venv {backend_argument / '.venv'}",
        f"pip install -r {backend_argument / 'requirements.txt'}",
    ]
    assert all("uvicorn" not in event for event in events)


def test_should_start_prepared_backend_as_foreground_process(tmp_path: Path):
    _setup, start, backend = _copy_backend_scripts(tmp_path)
    venv_bin = backend / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    pid_log = tmp_path / "uvicorn.pid"
    write_executable(venv_bin / "uvicorn", f'printf "%s" "$$" > "{pid_log}"')

    process = subprocess.Popen(_start_command(start), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.communicate(timeout=10)

    assert process.returncode == 0
    assert int(pid_log.read_text(encoding="utf-8")) == process.pid


def test_should_delegate_complete_uvicorn_arguments(tmp_path: Path):
    _setup, start, backend = _copy_backend_scripts(tmp_path)
    venv_bin = backend / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    arguments_log = tmp_path / "uvicorn-arguments.json"
    write_executable(
        venv_bin / "uvicorn",
        "python3 - \"$@\" <<'PY'\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(arguments_log)!r}, 'w'))\n"
        "PY\n",
    )

    result = subprocess.run(_start_command(start), capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert json.loads(arguments_log.read_text(encoding="utf-8")) == [
        "--app-dir",
        str(backend.parent / "scripts" / ".." / "backend"),
        "app.main:app",
        "--host",
        "localhost",
        "--port",
        "8000",
        "--reload",
    ]


def test_should_delegate_dogfood_uvicorn_arguments_without_reload(tmp_path: Path):
    _setup, start, backend = _copy_backend_scripts(tmp_path)
    venv_bin = backend / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    arguments_log = tmp_path / "uvicorn-arguments.json"
    write_executable(
        venv_bin / "uvicorn",
        "python3 - \"$@\" <<'PY'\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(arguments_log)!r}, 'w'))\n"
        "PY\n",
    )

    result = subprocess.run(
        _dogfood_start_command(start),
        env=_profile_environment("dogfood"),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    arguments = json.loads(arguments_log.read_text(encoding="utf-8"))
    assert arguments == [
        "--app-dir",
        str(backend.parent / "scripts" / ".." / "backend"),
        "app.main:app",
        "--host",
        "localhost",
        "--port",
        "18000",
    ]
    assert all(argument != "--reload" for argument in arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--host", "127.0.0.1", "--port", "18000"],
        ["--host", "localhost", "--port", "8000"],
        ["--host", "localhost", "--port", "18000", "--reload"],
    ],
)
def test_should_reject_backend_arguments_that_differ_from_resolved_profile(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    _setup, start, _backend = _copy_backend_scripts(tmp_path)

    result = subprocess.run(
        [str(start), *arguments],
        env=_profile_environment("dogfood"),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2


def test_should_preserve_callers_pythonpath_when_starting_backend(tmp_path: Path):
    _setup, start, backend = _copy_backend_scripts(tmp_path)
    venv_bin = backend / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    pythonpath_log = tmp_path / "pythonpath.log"
    write_executable(
        venv_bin / "uvicorn", f'printf "%s" "$PYTHONPATH" > "{pythonpath_log}"'
    )
    environment = {**os.environ, "PYTHONPATH": "/caller/import/root"}

    result = subprocess.run(_start_command(start), env=environment, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert pythonpath_log.read_text(encoding="utf-8") == "/caller/import/root"


def test_should_import_backend_clients_without_repository_root_on_pythonpath():
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT_DIR / "backend"),
    }

    result = subprocess.run(
        [
            "python3",
            "-c",
            "from app.llm.ollama_client import OllamaClient; "
            "from app.stt.whisper_client import WhisperTranscriber",
        ],
        cwd=ROOT_DIR / "backend",
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_should_exclude_repository_local_whisper_cache_from_git():
    from app.runtime_paths import resolve_runtime_paths

    generated_model = (
        resolve_runtime_paths({}, ROOT_DIR).whisper_cache_path
        / "models--example--converted-whisper"
        / "snapshots"
        / "generated-model"
    )

    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", str(generated_model)],
        cwd=ROOT_DIR,
    )

    assert result.returncode == 0


def test_should_fail_fast_when_backend_environment_is_not_prepared(tmp_path: Path):
    _setup, start, _backend = _copy_backend_scripts(tmp_path)

    result = subprocess.run(_start_command(start), capture_output=True, text=True)

    assert result.returncode != 0
    assert "setup-backend.sh" in result.stderr


def test_should_propagate_backend_process_status(tmp_path: Path):
    _setup, start, backend = _copy_backend_scripts(tmp_path)
    venv_bin = backend / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    write_executable(venv_bin / "uvicorn", "exit 29\n")

    result = subprocess.run(_start_command(start), capture_output=True, text=True)

    assert result.returncode == 29


def test_should_report_backend_dependency_install_failure_with_original_status(
    tmp_path: Path,
):
    setup, _start, backend = _copy_backend_scripts(tmp_path)
    (backend / "requirements.txt").write_text("# runtime\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(
        bin_dir / "python3",
        'venv_dir="${@: -1}"\nmkdir -p "$venv_dir/bin"\n'
        'printf "#!/usr/bin/env bash\\nexit 41\\n" > "$venv_dir/bin/pip"\n'
        'chmod +x "$venv_dir/bin/pip"\n',
    )
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = subprocess.run([str(setup)], env=env, capture_output=True, text=True)

    assert result.returncode == 41
    assert "dependency installation" in result.stderr.lower()


def test_should_preserve_resolved_profile_values_when_dotenv_conflicts(tmp_path: Path):
    _setup, start, backend = _copy_backend_scripts(tmp_path)
    venv_bin = backend / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    (backend / ".env").write_text(
        "OLLAMA_BASE_URL=http://dotenv.invalid:11434\n"
        "VOICEVOX_BASE_URL=http://dotenv.invalid:50021\n"
        "RAG_ENABLED=true\n"
        "OLLAMA_CHAT_MODEL=dotenv-invalid:1b\n"
        "WHISPER_MODEL=tiny\n"
        "OLLAMA_CONTEXT_TOKENS=2048\n",
        encoding="utf-8",
    )
    captured = tmp_path / "environment.json"
    write_executable(
        venv_bin / "uvicorn",
        "python3 - <<'PY'\n"
        "import json, os\n"
        f"json.dump({{key: os.environ[key] for key in "
        "['OLLAMA_BASE_URL', 'VOICEVOX_BASE_URL', 'RAG_ENABLED', "
        "'OLLAMA_CHAT_MODEL', 'WHISPER_MODEL', 'OLLAMA_CONTEXT_TOKENS']}, "
        f"open({str(captured)!r}, 'w'))\n"
        "PY\n",
    )
    data_root = tmp_path / "runtime-data"
    report = data_root / "runtime" / "standalone" / "resolved-profile.json"
    resolve = subprocess.run(
        [
            "python3",
            str(tmp_path / "environments" / "profile.py"),
            "resolve",
            "--report",
            str(report),
            "--default-profile",
            "dev",
        ],
        env={
            **os.environ,
            "DS_ENVIRONMENT_ID": "test",
            "DS_DATA_DIR": str(data_root),
            "OLLAMA_CHAT_MODEL": "profile-chat:12b",
            "WHISPER_MODEL": "large-v3",
            "OLLAMA_CONTEXT_TOKENS": "12288",
        },
        capture_output=True,
        text=True,
    )
    assert resolve.returncode == 0, resolve.stderr

    result = subprocess.run(
        _start_command(start),
        env={
            **os.environ,
            "DS_ENVIRONMENT_ID": "test",
            "DS_DATA_DIR": str(data_root),
            "DS_PROFILE_REPORT": str(report),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(captured.read_text(encoding="utf-8")) == {
        "OLLAMA_BASE_URL": "http://localhost:11434",
        "VOICEVOX_BASE_URL": "http://localhost:50021",
        "RAG_ENABLED": "false",
        "OLLAMA_CHAT_MODEL": "profile-chat:12b",
        "WHISPER_MODEL": "large-v3",
        "OLLAMA_CONTEXT_TOKENS": "12288",
    }


def test_should_resolve_dotenv_model_settings_before_starting_backend(tmp_path: Path):
    _setup, start, backend = _copy_backend_scripts(tmp_path)
    venv_bin = backend / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    (backend / ".env").write_text(
        "OLLAMA_CHAT_MODEL=dotenv-chat:9b\n"
        "WHISPER_MODEL=small\n"
        "OLLAMA_CONTEXT_TOKENS=12288\n",
        encoding="utf-8",
    )
    captured = tmp_path / "dotenv-environment.json"
    write_executable(
        venv_bin / "uvicorn",
        "python3 - <<'PY'\n"
        "import json, os\n"
        "keys = ['OLLAMA_CHAT_MODEL', 'WHISPER_MODEL', 'OLLAMA_CONTEXT_TOKENS']\n"
        f"json.dump({{key: os.environ[key] for key in keys}}, open({str(captured)!r}, 'w'))\n"
        "PY\n",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DS_PROFILE",
            "DS_PROFILE_REPORT",
            "CHAT_E2E_BACKEND",
            "VOICE_CHAT_E2E_BACKEND",
            "OLLAMA_CHAT_MODEL",
            "WHISPER_MODEL",
            "OLLAMA_CONTEXT_TOKENS",
        }
    }

    result = subprocess.run(
        _start_command(start), env=environment, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(captured.read_text(encoding="utf-8")) == {
        "OLLAMA_CHAT_MODEL": "dotenv-chat:9b",
        "WHISPER_MODEL": "small",
        "OLLAMA_CONTEXT_TOKENS": "12288",
    }
