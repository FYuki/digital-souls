import json
import os
import shutil
import subprocess
from pathlib import Path


_ROOT_DIR = Path(__file__).parent.parent.parent
_SCRIPTS_DIR = _ROOT_DIR / "scripts"
_SCRIPT_LIBRARIES = ["lib/process.sh", "lib/readiness.sh", "lib/profile.sh"]
_FORBIDDEN_COMMANDS = ["curl", "docker", "ollama", "pip", "python3", "sleep"]


def _copy_script(tmp_path: Path, name: str, backend_dir: Path | None = None) -> Path:
    content = (_SCRIPTS_DIR / name).read_text()
    if backend_dir is not None:
        content = content.replace(
            'BACKEND_DIR="$SCRIPT_DIR/../backend"',
            f'BACKEND_DIR="{backend_dir}"',
        )
    profile_aware = name in {"start-backend.sh", "start-voicevox.sh"}
    script_dir = tmp_path / "scripts" if profile_aware else tmp_path
    script_dir.mkdir(exist_ok=True)
    script = script_dir / name
    script.write_text(content)
    script.chmod(0o755)
    if profile_aware:
        lib_dir = script_dir / "lib"
        lib_dir.mkdir(exist_ok=True)
        for library in ["profile.sh", "readiness.sh"]:
            (lib_dir / library).write_text(
                (_SCRIPTS_DIR / "lib" / library).read_text()
            )
        if not (tmp_path / "environments").exists():
            shutil.copytree(_ROOT_DIR / "environments", tmp_path / "environments")
    return script


def _prepare_profile_backend_start(tmp_path: Path) -> tuple[Path, Path]:
    scripts_dir = tmp_path / "scripts"
    (scripts_dir / "lib").mkdir(parents=True)
    script = scripts_dir / "start-backend.sh"
    script.write_text((_SCRIPTS_DIR / "start-backend.sh").read_text())
    script.chmod(0o755)
    (scripts_dir / "lib" / "profile.sh").write_text(
        (_SCRIPTS_DIR / "lib" / "profile.sh").read_text()
    )
    shutil.copytree(_ROOT_DIR / "environments", tmp_path / "environments")
    return script, tmp_path / "backend"


def _resolve_profile_report(tmp_path: Path, report_path: Path) -> None:
    legacy_keys = {
        "DS_PROFILE",
        "DS_PROFILE_REPORT",
        "VOICE_CHAT_E2E_BACKEND",
        "CHAT_E2E_BACKEND",
        "CHAT_E2E_BACKEND_ORIGIN",
        "VOICE_CHAT_E2E_BACKEND_REPORT",
    }
    env = {key: value for key, value in os.environ.items() if key not in legacy_keys}
    result = subprocess.run(
        [
            "python3",
            str(tmp_path / "environments" / "profile.py"),
            "resolve",
            "--report",
            str(report_path),
            "--default-profile",
            "dev",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _write_command_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}")
    path.chmod(0o755)


def _isolated_path(tmp_path: Path, event_log: Path) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in _FORBIDDEN_COMMANDS:
        _write_command_stub(
            bin_dir / command,
            f'printf "%s\\n" "{command} $*" >> "{event_log}"\n',
        )
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return bin_dir, env


def _prepare_setup_backend(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    (backend_dir / "requirements.txt").write_text("# runtime dependencies\n")
    event_log = tmp_path / "events.log"
    bin_dir, env = _isolated_path(tmp_path, event_log)
    script = _copy_script(tmp_path, "setup-backend.sh", backend_dir)
    return script, backend_dir, event_log, env


def _write_python_venv_stub(bin_dir: Path, event_log: Path, pip_exit: int = 0) -> None:
    _write_command_stub(
        bin_dir / "python3",
        f'printf "%s\\n" "python3 $*" >> "{event_log}"\n'
        'venv_dir="${@: -1}"\n'
        'mkdir -p "$venv_dir/bin"\n'
        'cat > "$venv_dir/bin/pip" <<\'EOF\'\n'
        '#!/usr/bin/env bash\n'
        f'printf "%s\\n" "pip $*" >> "{event_log}"\n'
        f'exit {pip_exit}\n'
        'EOF\n'
        'chmod +x "$venv_dir/bin/pip"\n',
    )


def _prepare_orchestrator(tmp_path: Path, name: str) -> tuple[Path, Path, Path]:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / name
    script.write_text((_SCRIPTS_DIR / name).read_text())
    script.chmod(0o755)

    (scripts_dir / "lib").mkdir()
    for library in _SCRIPT_LIBRARIES:
        (scripts_dir / library).write_text((_SCRIPTS_DIR / library).read_text())

    shutil.copytree(_ROOT_DIR / "environments", tmp_path / "environments")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_command_stub(bin_dir / "curl", "exit 0\n")
    event_log = tmp_path / "events.log"
    _write_command_stub(bin_dir / "uvicorn", f'echo uvicorn-direct >> "{event_log}"\n')
    return script, bin_dir, event_log


def _write_service_stubs(scripts_dir: Path, event_log: Path) -> None:
    for name in [
        "setup-backend.sh",
        "start-ollama.sh",
        "start-voicevox.sh",
        "start-backend.sh",
        "start-frontend.sh",
    ]:
        _write_command_stub(
            scripts_dir / name,
            f'printf "%s\\n" "{name}" >> "{event_log}"\n',
        )


class TestBackendSetupContract:
    def test_should_create_venv_then_install_runtime_dependencies(self, tmp_path):
        script, backend_dir, event_log, env = _prepare_setup_backend(tmp_path)
        _write_python_venv_stub(tmp_path / "bin", event_log)

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 0
        assert event_log.read_text().splitlines() == [
            f"python3 -m venv {backend_dir / '.venv'}",
            f"pip install -r {backend_dir / 'requirements.txt'}",
        ]

    def test_should_report_venv_creation_failure_with_original_status(self, tmp_path):
        script, _, event_log, env = _prepare_setup_backend(tmp_path)
        _write_command_stub(
            tmp_path / "bin" / "python3",
            f'printf "%s\\n" "python3 $*" >> "{event_log}"\nexit 37\n',
        )

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 37
        assert "backend setup" in result.stderr.lower()
        assert "virtual environment creation" in result.stderr.lower()
        assert event_log.read_text().splitlines()[0].startswith("python3 -m venv ")

    def test_should_report_dependency_install_failure_with_original_status(self, tmp_path):
        script, _, event_log, env = _prepare_setup_backend(tmp_path)
        _write_python_venv_stub(tmp_path / "bin", event_log, pip_exit=41)

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 41
        assert "backend setup" in result.stderr.lower()
        assert "dependency installation" in result.stderr.lower()


class TestBackendStartContract:
    def test_should_use_only_prebuilt_backend_environment(self, tmp_path):
        backend_dir = tmp_path / "backend"
        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        event_log = tmp_path / "events.log"
        (venv_bin / "activate").write_text(
            f'printf "%s\\n" "activate" >> "{event_log}"\n'
        )
        _write_command_stub(
            venv_bin / "uvicorn",
            f'printf "%s\\n" "uvicorn $*" >> "{event_log}"\n',
        )
        script = _copy_script(tmp_path, "start-backend.sh", backend_dir)
        _write_command_stub(
            tmp_path / "setup-backend.sh",
            f'printf "%s\\n" "setup-backend.sh" >> "{event_log}"\n',
        )
        _, env = _isolated_path(tmp_path, event_log)
        python_stub = tmp_path / "bin" / "python3"
        python_stub.unlink()
        python_stub.symlink_to(shutil.which("python3"))

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 0
        assert event_log.read_text().splitlines() == [
            "activate",
            f"uvicorn --app-dir {backend_dir} app.main:app --reload",
        ]

    def test_should_replace_shell_with_foreground_backend_process(self, tmp_path):
        backend_dir = tmp_path / "backend"
        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "activate").write_text("")
        pid_log = tmp_path / "uvicorn.pid"
        _write_command_stub(venv_bin / "uvicorn", f'printf "%s" "$$" > "{pid_log}"\n')
        script = _copy_script(tmp_path, "start-backend.sh", backend_dir)

        process = subprocess.Popen([str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.communicate(timeout=10)

        assert process.returncode == 0
        assert int(pid_log.read_text()) == process.pid

    def test_should_propagate_backend_process_status(self, tmp_path):
        backend_dir = tmp_path / "backend"
        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "activate").write_text("")
        _write_command_stub(venv_bin / "uvicorn", "exit 29\n")
        script = _copy_script(tmp_path, "start-backend.sh", backend_dir)

        result = subprocess.run([str(script)], capture_output=True, text=True)

        assert result.returncode == 29

    def test_should_load_non_profile_dotenv_values_and_restore_profile_values(self, tmp_path):
        script, backend_dir = _prepare_profile_backend_start(tmp_path)
        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "activate").write_text("")
        (backend_dir / ".env").write_text(
            "OLLAMA_BASE_URL=http://dotenv.invalid:11434\n"
            "OLLAMA_EMBEDDING_MODEL=mxbai-embed-large:latest\n"
            "VOICEVOX_BASE_URL=http://dotenv.invalid:50021\n"
            "RAG_ENABLED=true\n"
            "DS_BACKEND_ORIGIN=http://dotenv.invalid:8000\n"
            "DS_PROFILE_REPORT=/tmp/dotenv-invalid-profile.json\n"
        )
        event_log = tmp_path / "environment.json"
        _write_command_stub(
            venv_bin / "uvicorn",
            "python3 - <<'PY'\n"
            "import json, os\n"
            f"with open({str(event_log)!r}, 'w') as output:\n"
            "    json.dump({key: os.environ[key] for key in "
            "['OLLAMA_BASE_URL', 'OLLAMA_EMBEDDING_MODEL', "
            "'VOICEVOX_BASE_URL', 'RAG_ENABLED', 'DS_BACKEND_ORIGIN']}, output)\n"
            "PY\n",
        )
        report_path = tmp_path / "resolved.json"
        _resolve_profile_report(tmp_path, report_path)
        env = {
            **os.environ,
            "DS_PROFILE_REPORT": str(report_path),
        }

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 0, result.stderr
        assert json.loads(event_log.read_text()) == {
            "OLLAMA_BASE_URL": "http://localhost:11434",
            "OLLAMA_EMBEDDING_MODEL": "mxbai-embed-large:latest",
            "VOICEVOX_BASE_URL": "http://localhost:50021",
            "RAG_ENABLED": "false",
            "DS_BACKEND_ORIGIN": "http://localhost:8000",
        }

    def test_should_restore_profile_values_after_loading_backend_dotenv(self, tmp_path):
        backend_dir = tmp_path / "backend"
        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "activate").write_text("")
        (backend_dir / ".env").write_text("OLLAMA_BASE_URL=http://dotenv.example:11434\n")
        event_log = tmp_path / "ollama-url.txt"
        _write_command_stub(
            venv_bin / "uvicorn",
            f'printf "%s" "$OLLAMA_BASE_URL" > "{event_log}"\n',
        )
        script = _copy_script(tmp_path, "start-backend.sh", backend_dir)
        env = {key: value for key, value in os.environ.items() if key != "DS_PROFILE_REPORT"}
        env.pop("OLLAMA_BASE_URL", None)

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 0, result.stderr
        assert event_log.read_text() == "http://localhost:11434"

    def test_should_fail_clearly_without_activate_and_not_run_setup(self, tmp_path):
        backend_dir = tmp_path / "backend"
        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        _write_command_stub(venv_bin / "uvicorn", "exit 0\n")
        setup_marker = tmp_path / "setup-called"
        _write_command_stub(tmp_path / "setup-backend.sh", f'touch "{setup_marker}"\n')
        script = _copy_script(tmp_path, "start-backend.sh", backend_dir)

        result = subprocess.run([str(script)], capture_output=True, text=True)

        assert result.returncode != 0
        assert "activate" in result.stderr.lower()
        assert "setup-backend.sh" in result.stderr
        assert not setup_marker.exists()

    def test_should_fail_clearly_without_uvicorn_and_not_run_setup(self, tmp_path):
        backend_dir = tmp_path / "backend"
        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "activate").write_text("")
        setup_marker = tmp_path / "setup-called"
        _write_command_stub(tmp_path / "setup-backend.sh", f'touch "{setup_marker}"\n')
        script = _copy_script(tmp_path, "start-backend.sh", backend_dir)

        result = subprocess.run([str(script)], capture_output=True, text=True)

        assert result.returncode != 0
        assert "uvicorn" in result.stderr.lower()
        assert "setup-backend.sh" in result.stderr
        assert not setup_marker.exists()


class TestVoicevoxStartContract:
    def test_should_preserve_profile_voicevox_url_when_dotenv_conflicts(self, tmp_path):
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()
        (backend_dir / ".env").write_text(
            "VOICEVOX_BASE_URL=http://dotenv.invalid:50021\n"
        )
        script = _copy_script(tmp_path, "start-voicevox.sh", backend_dir)
        event_log = tmp_path / "events.log"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_command_stub(bin_dir / "docker", "exit 0\n")
        _write_command_stub(
            bin_dir / "curl",
            f'printf "%s\\n" "curl $*" >> "{event_log}"\n',
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DS_PROFILE_REPORT": str(tmp_path / "resolved.json"),
        }

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 0, result.stderr
        assert event_log.read_text().splitlines() == [
            "curl -sf http://localhost:50021/version"
        ]


class TestBackendOrchestrationContract:
    def test_should_setup_before_all_development_services(self, tmp_path):
        script, bin_dir, event_log = _prepare_orchestrator(tmp_path, "start-all.sh")
        _write_service_stubs(script.parent, event_log)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 0
        assert event_log.read_text().splitlines() == [
            "setup-backend.sh",
            "start-ollama.sh",
            "start-voicevox.sh",
            "start-backend.sh",
            "start-frontend.sh",
        ]

    def test_should_stop_development_flow_when_setup_fails(self, tmp_path):
        script, bin_dir, event_log = _prepare_orchestrator(tmp_path, "start-all.sh")
        _write_service_stubs(script.parent, event_log)
        _write_command_stub(
            script.parent / "setup-backend.sh",
            f'printf "%s\\n" "setup-backend.sh" >> "{event_log}"\nexit 47\n',
        )
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 47
        assert event_log.read_text().splitlines() == ["setup-backend.sh"]

    def test_should_setup_before_all_real_e2e_services(self, tmp_path):
        script, bin_dir, event_log = _prepare_orchestrator(
            tmp_path, "start-voice-chat-e2e.sh"
        )
        _write_service_stubs(script.parent, event_log)
        report_path = tmp_path / "backend-report.json"
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VOICE_CHAT_E2E_BACKEND": "real",
            "VOICE_CHAT_E2E_BACKEND_REPORT": str(report_path),
        }

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 0
        assert event_log.read_text().splitlines() == [
            "setup-backend.sh",
            "start-ollama.sh",
            "start-voicevox.sh",
            "start-backend.sh",
            "start-frontend.sh",
        ]

    def test_should_stop_real_e2e_flow_when_setup_fails(self, tmp_path):
        script, bin_dir, event_log = _prepare_orchestrator(
            tmp_path, "start-voice-chat-e2e.sh"
        )
        _write_service_stubs(script.parent, event_log)
        _write_command_stub(
            script.parent / "setup-backend.sh",
            f'printf "%s\\n" "setup-backend.sh" >> "{event_log}"\nexit 53\n',
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VOICE_CHAT_E2E_BACKEND": "real",
            "VOICE_CHAT_E2E_BACKEND_REPORT": str(tmp_path / "backend-report.json"),
        }

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 53
        assert event_log.read_text().splitlines() == ["setup-backend.sh"]
