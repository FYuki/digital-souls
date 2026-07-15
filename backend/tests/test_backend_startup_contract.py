import os
import subprocess
from pathlib import Path


_ROOT_DIR = Path(__file__).parent.parent.parent
_SCRIPTS_DIR = _ROOT_DIR / "scripts"
_SCRIPT_LIBRARIES = ["lib/process.sh", "lib/readiness.sh"]
_FORBIDDEN_COMMANDS = ["curl", "docker", "ollama", "pip", "python3", "sleep"]


def _copy_script(tmp_path: Path, name: str, backend_dir: Path | None = None) -> Path:
    content = (_SCRIPTS_DIR / name).read_text()
    if backend_dir is not None:
        content = content.replace(
            'BACKEND_DIR="$SCRIPT_DIR/../backend"',
            f'BACKEND_DIR="{backend_dir}"',
        )
    script = tmp_path / name
    script.write_text(content)
    script.chmod(0o755)
    return script


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
