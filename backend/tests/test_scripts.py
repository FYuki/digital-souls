import os
import subprocess
from pathlib import Path

import pytest


_ROOT_DIR = Path(__file__).parent.parent.parent
_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"

_SCRIPTS = [
    "start-all.sh",
    "start-backend.sh",
    "start-frontend.sh",
    "start-ollama.sh",
]


class TestScriptStructure:
    def test_all_scripts_exist(self):
        for name in _SCRIPTS:
            assert (_SCRIPTS_DIR / name).exists(), f"{name} not found"

    def test_all_scripts_are_executable(self):
        for name in _SCRIPTS:
            path = _SCRIPTS_DIR / name
            assert os.access(path, os.X_OK), f"{name} is not executable"

    def test_all_scripts_have_strict_mode(self):
        for name in _SCRIPTS:
            content = (_SCRIPTS_DIR / name).read_text()
            assert "set -euo pipefail" in content, f"{name} missing set -euo pipefail"

    def test_all_scripts_pass_bash_syntax_check(self):
        for name in _SCRIPTS:
            result = subprocess.run(
                ["bash", "-n", str(_SCRIPTS_DIR / name)],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"{name} failed bash -n: {result.stderr}"

    def test_gitignore_excludes_backend_venv(self):
        content = (_ROOT_DIR / ".gitignore").read_text().splitlines()
        assert ".venv/" in content

    def test_dev_requirements_include_runtime_requirements(self):
        content = (_ROOT_DIR / "backend/requirements-dev.txt").read_text().splitlines()
        assert "-r requirements.txt" in content


class TestStartFrontend:
    def test_exits_successfully(self):
        result = subprocess.run(
            [str(_SCRIPTS_DIR / "start-frontend.sh")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_prints_skip_message(self):
        result = subprocess.run(
            [str(_SCRIPTS_DIR / "start-frontend.sh")],
            capture_output=True,
            text=True,
        )
        assert "not yet implemented" in result.stdout.lower() or "skipping" in result.stdout.lower()


def _make_uvicorn_stub(bin_dir: Path, log_file: Path) -> None:
    stub = bin_dir / "uvicorn"
    stub.write_text(f'#!/usr/bin/env bash\necho "uvicorn" >> "{log_file}"\n')
    stub.chmod(0o755)


def _make_modified_start_backend(tmp_path: Path, backend_dir: Path) -> Path:
    content = (_SCRIPTS_DIR / "start-backend.sh").read_text()
    content = content.replace(
        'BACKEND_DIR="$SCRIPT_DIR/../backend"',
        f'BACKEND_DIR="{backend_dir}"',
    )
    script = tmp_path / "start-backend.sh"
    script.write_text(content)
    script.chmod(0o755)
    return script


class TestStartBackend:
    def test_env_file_variables_are_exported_to_process(self, tmp_path):
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()
        (backend_dir / ".env").write_text("TEST_MARKER=loaded_from_env\n")

        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "activate").write_text("# stub activate\n")

        env_log = tmp_path / "env.txt"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        uvicorn = bin_dir / "uvicorn"
        uvicorn.write_text(f'#!/usr/bin/env bash\nenv > "{env_log}"\n')
        uvicorn.chmod(0o755)

        script = _make_modified_start_backend(tmp_path, backend_dir)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert env_log.exists(), "uvicorn stub was not called"
        assert "TEST_MARKER=loaded_from_env" in env_log.read_text()

    def test_venv_is_activated_before_uvicorn(self, tmp_path):
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()

        order_log = tmp_path / "order.txt"

        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "activate").write_text(f'echo "activate" >> "{order_log}"\n')

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _make_uvicorn_stub(bin_dir, order_log)

        script = _make_modified_start_backend(tmp_path, backend_dir)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        subprocess.run([str(script)], env=env, capture_output=True, text=True)

        order = order_log.read_text().splitlines()
        assert "activate" in order, "venv activate was not sourced"
        assert "uvicorn" in order, "uvicorn was not called"
        assert order.index("activate") < order.index("uvicorn")

    def test_uvicorn_is_called_with_backend_app_and_reload(self, tmp_path):
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()

        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "activate").write_text("# stub activate\n")

        args_log = tmp_path / "uvicorn_args.txt"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        uvicorn = bin_dir / "uvicorn"
        uvicorn.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$@\" > \"{args_log}\"\n"
        )
        uvicorn.chmod(0o755)

        script = _make_modified_start_backend(tmp_path, backend_dir)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert args_log.read_text().splitlines() == [
            "--app-dir",
            str(backend_dir),
            "app.main:app",
            "--reload",
        ]


def _make_start_all_stub_env(tmp_path: Path, curl_exit: int, max_attempts: int = 30):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    content = (_SCRIPTS_DIR / "start-all.sh").read_text()
    content = content.replace("local max_attempts=30", f"local max_attempts={max_attempts}")
    (scripts_dir / "start-all.sh").write_text(content)
    (scripts_dir / "start-all.sh").chmod(0o755)

    for name in ["start-ollama.sh", "start-backend.sh", "start-frontend.sh"]:
        stub = scripts_dir / name
        stub.write_text("#!/usr/bin/env bash\n")
        stub.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(f"#!/usr/bin/env bash\nexit {curl_exit}\n")
    (bin_dir / "curl").chmod(0o755)

    return scripts_dir, bin_dir


class TestStartAll:
    def test_starts_services_in_order_ollama_backend_frontend(self, tmp_path):
        scripts_dir, bin_dir = _make_start_all_stub_env(tmp_path, curl_exit=0)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(scripts_dir / "start-all.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        stdout = result.stdout
        ollama_pos = stdout.find("==> Starting Ollama...")
        backend_pos = stdout.find("==> Starting Backend...")
        frontend_pos = stdout.find("==> Starting Frontend...")

        assert ollama_pos != -1, "Ollama start message not found in stdout"
        assert backend_pos != -1, "Backend start message not found in stdout"
        assert frontend_pos != -1, "Frontend start message not found in stdout"
        assert ollama_pos < backend_pos < frontend_pos

    def test_start_all_cleans_up_started_processes_when_ollama_check_times_out(self, tmp_path):
        scripts_dir, bin_dir = _make_start_all_stub_env(tmp_path, curl_exit=1, max_attempts=1)
        pid_file = tmp_path / "ollama.pid"

        (scripts_dir / "start-ollama.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo $$ > \"{pid_file}\"\n"
            f"exec sleep 30\n"
        )
        (scripts_dir / "start-ollama.sh").chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(scripts_dir / "start-all.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode != 0
        pid = int(pid_file.read_text().strip())

        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_ollama_health_check_uses_api_tags_endpoint(self):
        content = (_SCRIPTS_DIR / "start-all.sh").read_text()
        assert "localhost:11434/api/tags" in content, (
            "Ollama health check must target /api/tags, not root /"
        )

    def test_wait_for_http_exits_nonzero_on_timeout(self, tmp_path):
        scripts_dir, bin_dir = _make_start_all_stub_env(
            tmp_path, curl_exit=1, max_attempts=1
        )
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(scripts_dir / "start-all.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode != 0
        assert "ERROR" in result.stderr

    def test_does_not_start_frontend_when_backend_check_times_out(self, tmp_path):
        scripts_dir, bin_dir = _make_start_all_stub_env(
            tmp_path, curl_exit=0, max_attempts=1
        )
        frontend_marker = tmp_path / "frontend_started.txt"

        (scripts_dir / "start-ollama.sh").write_text(
            "#!/usr/bin/env bash\nexec sleep 30\n"
        )
        (scripts_dir / "start-ollama.sh").chmod(0o755)
        (scripts_dir / "start-backend.sh").write_text(
            "#!/usr/bin/env bash\nexec sleep 30\n"
        )
        (scripts_dir / "start-backend.sh").chmod(0o755)
        (scripts_dir / "start-frontend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"touch \"{frontend_marker}\"\n"
        )
        (scripts_dir / "start-frontend.sh").chmod(0o755)
        (bin_dir / "curl").write_text(
            "#!/usr/bin/env bash\n"
            "url=\"${@: -1}\"\n"
            "case \"$url\" in\n"
            "  *localhost:11434*) exit 0 ;;\n"
            "  *localhost:8000*) exit 1 ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        (bin_dir / "curl").chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(scripts_dir / "start-all.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode != 0
        assert not frontend_marker.exists()

    def test_returns_nonzero_when_started_child_exits_nonzero(self, tmp_path):
        scripts_dir, bin_dir = _make_start_all_stub_env(tmp_path, curl_exit=0)

        (scripts_dir / "start-ollama.sh").write_text("#!/usr/bin/env bash\nexit 7\n")
        (scripts_dir / "start-ollama.sh").chmod(0o755)
        (scripts_dir / "start-backend.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (scripts_dir / "start-backend.sh").chmod(0o755)
        (scripts_dir / "start-frontend.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (scripts_dir / "start-frontend.sh").chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(scripts_dir / "start-all.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 7

    def test_fails_when_child_exits_before_health_check_succeeds(self, tmp_path):
        scripts_dir, bin_dir = _make_start_all_stub_env(
            tmp_path, curl_exit=1, max_attempts=30
        )

        (scripts_dir / "start-ollama.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (scripts_dir / "start-ollama.sh").chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(scripts_dir / "start-all.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode != 0
        assert "process exited before becoming ready" in result.stderr


class TestStartOllama:
    def test_invokes_ollama_serve(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_file = tmp_path / "ollama.log"

        ollama = bin_dir / "ollama"
        ollama.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" > \"{log_file}\"\n"
            f"exit 42\n"
        )
        ollama.chmod(0o755)
        curl = bin_dir / "curl"
        curl.write_text("#!/usr/bin/env bash\nexit 1\n")
        curl.chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(_SCRIPTS_DIR / "start-ollama.sh")],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 42
        assert log_file.read_text().strip() == "serve"
