from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT_DIR = Path(__file__).parent.parent.parent


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _copy_backend_scripts(tmp_path: Path) -> tuple[Path, Path, Path]:
    scripts = tmp_path / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(ROOT_DIR / "scripts" / "lib" / "profile.sh", scripts / "lib" / "profile.sh")
    shutil.copytree(ROOT_DIR / "environments", tmp_path / "environments")
    backend = tmp_path / "backend"
    backend.mkdir()
    for name in ("setup-backend.sh", "start-backend.sh"):
        source = (ROOT_DIR / "scripts" / name).read_text(encoding="utf-8")
        source = source.replace(
            'BACKEND_DIR="$SCRIPT_DIR/../backend"', f'BACKEND_DIR="{backend}"'
        )
        (scripts / name).write_text(source, encoding="utf-8")
        (scripts / name).chmod(0o755)
    return scripts / "setup-backend.sh", scripts / "start-backend.sh", backend


def test_should_prepare_backend_before_start_without_starting_uvicorn(tmp_path: Path):
    setup, _start, backend = _copy_backend_scripts(tmp_path)
    (backend / "requirements.txt").write_text("# runtime\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    event_log = tmp_path / "events.log"
    _write_executable(
        bin_dir / "python3",
        f'printf "%s\\n" "python $*" >> "{event_log}"\n'
        'venv_dir="${@: -1}"\nmkdir -p "$venv_dir/bin"\n'
        f'printf "#!/usr/bin/env bash\\nprintf \'%s\\\\n\' \'pip $*\' >> \'{event_log}\'\\n" > "$venv_dir/bin/pip"\n'
        'chmod +x "$venv_dir/bin/pip"\n',
    )
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = subprocess.run([str(setup)], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert event_log.read_text(encoding="utf-8").splitlines()[0].startswith("python -m venv")
    assert all("uvicorn" not in event for event in event_log.read_text().splitlines())


def test_should_start_prepared_backend_as_foreground_process(tmp_path: Path):
    _setup, start, backend = _copy_backend_scripts(tmp_path)
    venv_bin = backend / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    pid_log = tmp_path / "uvicorn.pid"
    _write_executable(venv_bin / "uvicorn", f'printf "%s" "$$" > "{pid_log}"')

    process = subprocess.Popen([str(start)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.communicate(timeout=10)

    assert process.returncode == 0
    assert int(pid_log.read_text(encoding="utf-8")) == process.pid


def test_should_preserve_callers_pythonpath_when_starting_backend(tmp_path: Path):
    _setup, start, backend = _copy_backend_scripts(tmp_path)
    venv_bin = backend / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    pythonpath_log = tmp_path / "pythonpath.log"
    _write_executable(
        venv_bin / "uvicorn", f'printf "%s" "$PYTHONPATH" > "{pythonpath_log}"'
    )
    environment = {**os.environ, "PYTHONPATH": "/caller/import/root"}

    result = subprocess.run([str(start)], env=environment, capture_output=True, text=True)

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
    from app.model_settings import (
        WHISPER_MODEL_CACHE_DIRECTORY,
        whisper_model_cache,
    )

    generated_model = (
        whisper_model_cache(ROOT_DIR)
        / WHISPER_MODEL_CACHE_DIRECTORY
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

    result = subprocess.run([str(start)], capture_output=True, text=True)

    assert result.returncode != 0
    assert "setup-backend.sh" in result.stderr


def test_should_propagate_backend_process_status(tmp_path: Path):
    _setup, start, backend = _copy_backend_scripts(tmp_path)
    venv_bin = backend / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("", encoding="utf-8")
    _write_executable(venv_bin / "uvicorn", "exit 29\n")

    result = subprocess.run([str(start)], capture_output=True, text=True)

    assert result.returncode == 29


def test_should_report_backend_dependency_install_failure_with_original_status(
    tmp_path: Path,
):
    setup, _start, backend = _copy_backend_scripts(tmp_path)
    (backend / "requirements.txt").write_text("# runtime\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
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
        "RAG_ENABLED=true\n",
        encoding="utf-8",
    )
    captured = tmp_path / "environment.json"
    _write_executable(
        venv_bin / "uvicorn",
        "python3 - <<'PY'\n"
        "import json, os\n"
        f"json.dump({{key: os.environ[key] for key in "
        "['OLLAMA_BASE_URL', 'VOICEVOX_BASE_URL', 'RAG_ENABLED']}, "
        f"open({str(captured)!r}, 'w'))\n"
        "PY\n",
    )
    report = tmp_path / "resolved-profile.json"
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
        capture_output=True,
        text=True,
    )
    assert resolve.returncode == 0, resolve.stderr

    result = subprocess.run(
        [str(start)],
        env={**os.environ, "DS_PROFILE_REPORT": str(report)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(captured.read_text(encoding="utf-8")) == {
        "OLLAMA_BASE_URL": "http://localhost:11434",
        "VOICEVOX_BASE_URL": "http://localhost:50021",
        "RAG_ENABLED": "false",
    }
