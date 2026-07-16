import os
import shutil
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
    "start-voicevox.sh",
    "start-voice-chat-e2e.sh",
    "setup-backend.sh",
]

_SCRIPT_LIBRARIES = [
    "lib/process.sh",
    "lib/readiness.sh",
    "lib/profile.sh",
]

_PROFILE_ENV_TO_CLEAR = {
    "DS_PROFILE",
    "DS_PROFILE_REPORT",
    "DS_BACKEND_ORIGIN",
    "VOICE_CHAT_E2E_BACKEND",
    "CHAT_E2E_BACKEND",
    "CHAT_E2E_BACKEND_ORIGIN",
    "VOICE_CHAT_E2E_BACKEND_REPORT",
}


def _without_profile_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _PROFILE_ENV_TO_CLEAR
    }


class TestScriptStructure:
    def test_all_scripts_exist(self):
        for name in _SCRIPTS:
            assert (_SCRIPTS_DIR / name).exists(), f"{name} not found"

        for name in _SCRIPT_LIBRARIES:
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
        for name in _SCRIPTS + _SCRIPT_LIBRARIES:
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

    def test_chat_e2e_startup_scripts_are_not_present(self):
        assert not (_SCRIPTS_DIR / "start-chat-e2e.sh").exists()
        assert not (_SCRIPTS_DIR / "chat-e2e-backend.sh").exists()


class TestDevelopmentEnvironmentDocs:
    def test_initial_setup_includes_backend_venv_prerequisites(self):
        setup_backend = (_SCRIPTS_DIR / "setup-backend.sh").read_text()
        docs = (_ROOT_DIR / "docs/development-environment.md").read_text()

        assert "python3 -m venv" in setup_backend
        assert "python3" in docs
        assert "python3-venv" in docs


class TestStartFrontend:
    def test_invokes_frontend_dev_server(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        npm_log = tmp_path / "npm.log"
        npm = bin_dir / "npm"
        npm.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$@\" > \"{npm_log}\"\n"
        )
        npm.chmod(0o755)

        report_path = tmp_path / "resolved-profile.json"
        env = {
            **_without_profile_environment(),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DS_PROFILE_REPORT": str(report_path),
        }

        result = subprocess.run(
            [str(_SCRIPTS_DIR / "start-frontend.sh")],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert npm_log.read_text().splitlines() == [
            "run",
            "dev",
            "--prefix",
            str(_SCRIPTS_DIR / "../frontend"),
        ]
        assert report_path.exists()

    def test_reuses_parent_resolved_report_without_resolving_again(self, tmp_path):
        report_path = tmp_path / "resolved-profile.json"
        resolve_result = subprocess.run(
            [
                "python3",
                str(_ROOT_DIR / "environments/profile.py"),
                "resolve",
                "--report",
                str(report_path),
            ],
            env={**_without_profile_environment(), "DS_PROFILE": "test-mocked"},
            capture_output=True,
            text=True,
        )
        assert resolve_result.returncode == 0, resolve_result.stderr
        original_report = report_path.read_text()

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        npm = bin_dir / "npm"
        npm.write_text("#!/usr/bin/env bash\nexit 0\n")
        npm.chmod(0o755)
        env = {
            **_without_profile_environment(),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DS_PROFILE_REPORT": str(report_path),
        }

        result = subprocess.run(
            [str(_SCRIPTS_DIR / "start-frontend.sh")],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert report_path.read_text() == original_report

    def test_standalone_start_loads_real_vite_config(self):
        env = _without_profile_environment()

        result = subprocess.run(
            ["timeout", "8", str(_SCRIPTS_DIR / "start-frontend.sh")],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 124, result.stdout + result.stderr
        assert "VITE" in result.stdout
        assert "ready in" in result.stdout
        assert "DS_PROFILE_REPORT must identify" not in result.stderr


def _make_modified_setup_backend(tmp_path: Path, backend_dir: Path) -> Path:
    content = (_SCRIPTS_DIR / "setup-backend.sh").read_text()
    content = content.replace(
        'BACKEND_DIR="$SCRIPT_DIR/../backend"',
        f'BACKEND_DIR="{backend_dir}"',
    )
    script = tmp_path / "setup-backend.sh"
    script.write_text(content)
    script.chmod(0o755)
    return script


class TestSetupBackend:
    def test_creates_venv_and_installs_runtime_requirements_when_venv_is_absent(
        self, tmp_path
    ):
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()
        requirements = backend_dir / "requirements.txt"
        requirements.write_text("# runtime requirements\n")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        python_log = tmp_path / "python_args.txt"
        pip_log = tmp_path / "pip_args.txt"
        python = bin_dir / "python3"
        python.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$@\" > \"{python_log}\"\n"
            "venv_dir=\"${@: -1}\"\n"
            "mkdir -p \"$venv_dir/bin\"\n"
            f"cat > \"$venv_dir/bin/pip\" <<'EOF'\n"
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$@\" > \"{pip_log}\"\n"
            "EOF\n"
            "chmod +x \"$venv_dir/bin/pip\"\n"
        )
        python.chmod(0o755)

        script = _make_modified_setup_backend(tmp_path, backend_dir)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 0
        assert python_log.read_text().splitlines() == [
            "-m",
            "venv",
            str(backend_dir / ".venv"),
        ]
        assert pip_log.read_text().splitlines() == [
            "install",
            "-r",
            str(requirements),
        ]

    def test_reuses_existing_venv_and_installs_runtime_requirements(self, tmp_path):
        backend_dir = tmp_path / "backend"
        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        requirements = backend_dir / "requirements.txt"
        requirements.write_text("# runtime requirements\n")

        pip_log = tmp_path / "pip_args.txt"
        pip = venv_bin / "pip"
        pip.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"{pip_log}\"\n")
        pip.chmod(0o755)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        python_log = tmp_path / "python_called.txt"
        python = bin_dir / "python3"
        python.write_text(f"#!/usr/bin/env bash\necho called > \"{python_log}\"\n")
        python.chmod(0o755)

        script = _make_modified_setup_backend(tmp_path, backend_dir)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 0
        assert not python_log.exists()
        assert pip_log.read_text().splitlines() == [
            "install",
            "-r",
            str(requirements),
        ]


def _make_uvicorn_stub(bin_dir: Path, log_file: Path) -> None:
    stub = bin_dir / "uvicorn"
    stub.write_text(f'#!/usr/bin/env bash\necho "uvicorn" >> "{log_file}"\n')
    stub.chmod(0o755)


def _make_venv_uvicorn_stub(venv_bin: Path, content: str) -> Path:
    stub = venv_bin / "uvicorn"
    stub.write_text(content)
    stub.chmod(0o755)
    return stub


def _make_modified_start_backend(tmp_path: Path, backend_dir: Path) -> Path:
    content = (_SCRIPTS_DIR / "start-backend.sh").read_text()
    content = content.replace(
        'BACKEND_DIR="$SCRIPT_DIR/../backend"',
        f'BACKEND_DIR="{backend_dir}"',
    )
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    lib_dir = scripts_dir / "lib"
    lib_dir.mkdir()
    (lib_dir / "profile.sh").write_text(
        (_SCRIPTS_DIR / "lib" / "profile.sh").read_text()
    )
    shutil.copytree(_ROOT_DIR / "environments", tmp_path / "environments")
    script = scripts_dir / "start-backend.sh"
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
        _make_venv_uvicorn_stub(venv_bin, f'#!/usr/bin/env bash\nenv > "{env_log}"\n')

        script = _make_modified_start_backend(tmp_path, backend_dir)

        subprocess.run([str(script)], env=os.environ, capture_output=True, text=True)

        assert env_log.exists(), "uvicorn stub was not called"
        assert "TEST_MARKER=loaded_from_env" in env_log.read_text()

    def test_venv_is_activated_before_uvicorn(self, tmp_path):
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()

        order_log = tmp_path / "order.txt"

        venv_bin = backend_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "activate").write_text(f'echo "activate" >> "{order_log}"\n')
        _make_venv_uvicorn_stub(
            venv_bin,
            f'#!/usr/bin/env bash\necho "uvicorn" >> "{order_log}"\n',
        )

        script = _make_modified_start_backend(tmp_path, backend_dir)

        subprocess.run([str(script)], env=os.environ, capture_output=True, text=True)

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
        _make_venv_uvicorn_stub(
            venv_bin,
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$@\" > \"{args_log}\"\n",
        )

        script = _make_modified_start_backend(tmp_path, backend_dir)

        subprocess.run([str(script)], env=os.environ, capture_output=True, text=True)

        assert args_log.read_text().splitlines() == [
            "--app-dir",
            str(backend_dir),
            "app.main:app",
            "--reload",
        ]

    def test_backend_venv_is_required(self, tmp_path):
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _make_uvicorn_stub(bin_dir, tmp_path / "uvicorn_called.txt")

        script = _make_modified_start_backend(tmp_path, backend_dir)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

        assert result.returncode == 1
        assert "backend virtualenv is required" in result.stderr
        assert not (tmp_path / "uvicorn_called.txt").exists()


def _make_start_all_stub_env(tmp_path: Path, curl_exit: int, max_attempts: int = 30):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    content = (_SCRIPTS_DIR / "start-all.sh").read_text()
    content = content.replace(
        "set -euo pipefail",
        "set -euo pipefail\n"
        f"export HTTP_READINESS_MAX_ATTEMPTS={max_attempts}\n"
        "export HTTP_READINESS_INTERVAL_SECONDS=0",
        1,
    )
    (scripts_dir / "start-all.sh").write_text(content)
    (scripts_dir / "start-all.sh").chmod(0o755)

    lib_dir = scripts_dir / "lib"
    lib_dir.mkdir()
    for name in _SCRIPT_LIBRARIES:
        (scripts_dir / name).write_text((_SCRIPTS_DIR / name).read_text())
    shutil.copytree(_ROOT_DIR / "environments", tmp_path / "environments")

    for name in [
        "setup-backend.sh",
        "start-ollama.sh",
        "start-voicevox.sh",
        "start-backend.sh",
        "start-frontend.sh",
    ]:
        stub = scripts_dir / name
        stub.write_text("#!/usr/bin/env bash\n")
        stub.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(f"#!/usr/bin/env bash\nexit {curl_exit}\n")
    (bin_dir / "curl").chmod(0o755)

    return scripts_dir, bin_dir


class TestStartAll:
    def test_uses_shared_process_and_readiness_libraries(self):
        content = (_SCRIPTS_DIR / "start-all.sh").read_text()

        assert 'source "$SCRIPT_DIR/lib/process.sh"' in content
        assert 'source "$SCRIPT_DIR/lib/readiness.sh"' in content
        assert "process_manager_init" in content
        assert 'source "$SCRIPT_DIR/lib/profile.sh"' in content
        assert 'profile_resolve "dev"' in content
        assert "profile_start_stack" in content

    def test_does_not_keep_local_process_or_readiness_implementations(self):
        content = (_SCRIPTS_DIR / "start-all.sh").read_text()

        for obsolete in [
            "CHILD_PIDS=",
            "_CLEANED_UP=",
            "cleanup()",
            "handle_signal()",
            "_start_child()",
            "_wait_for_children()",
            "_wait_for_http()",
        ]:
            assert obsolete not in content

    def test_starts_services_after_backend_setup(self, tmp_path):
        scripts_dir, bin_dir = _make_start_all_stub_env(tmp_path, curl_exit=0)
        order_log = tmp_path / "order.log"
        for name in [
            "setup-backend.sh",
            "start-ollama.sh",
            "start-voicevox.sh",
            "start-backend.sh",
            "start-frontend.sh",
        ]:
            (scripts_dir / name).write_text(
                f"#!/usr/bin/env bash\n"
                f"echo {name} >> \"{order_log}\"\n"
            )
            (scripts_dir / name).chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(scripts_dir / "start-all.sh")],
            cwd=tmp_path.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert order_log.read_text().splitlines() == [
            "setup-backend.sh",
            "start-ollama.sh",
            "start-voicevox.sh",
            "start-backend.sh",
            "start-frontend.sh",
        ]

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
        content = (_ROOT_DIR / "environments/profiles/dev.json").read_text()
        assert '"readinessPath": "/api/tags"' in content, (
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

    def test_does_not_start_backend_or_frontend_when_voicevox_start_fails(self, tmp_path):
        scripts_dir, bin_dir = _make_start_all_stub_env(
            tmp_path, curl_exit=0, max_attempts=1
        )
        backend_marker = tmp_path / "backend_started.txt"
        frontend_marker = tmp_path / "frontend_started.txt"

        (scripts_dir / "start-ollama.sh").write_text(
            "#!/usr/bin/env bash\nexec sleep 30\n"
        )
        (scripts_dir / "start-ollama.sh").chmod(0o755)
        (scripts_dir / "start-voicevox.sh").write_text("#!/usr/bin/env bash\nexit 12\n")
        (scripts_dir / "start-voicevox.sh").chmod(0o755)
        (scripts_dir / "start-backend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"touch \"{backend_marker}\"\n"
        )
        (scripts_dir / "start-backend.sh").chmod(0o755)
        (scripts_dir / "start-frontend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"touch \"{frontend_marker}\"\n"
        )
        (scripts_dir / "start-frontend.sh").chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(scripts_dir / "start-all.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 12
        assert not backend_marker.exists()
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


class TestStartVoicevox:
    def _copy_script(self, tmp_path):
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "start-voicevox.sh"
        script.write_text((_SCRIPTS_DIR / "start-voicevox.sh").read_text())
        script.chmod(0o755)
        lib_dir = scripts_dir / "lib"
        lib_dir.mkdir()
        (lib_dir / "readiness.sh").write_text(
            (_SCRIPTS_DIR / "lib/readiness.sh").read_text()
        )
        (lib_dir / "profile.sh").write_text(
            (_SCRIPTS_DIR / "lib/profile.sh").read_text()
        )
        shutil.copytree(_ROOT_DIR / "environments", tmp_path / "environments")
        return script

    def test_uses_shared_readiness_directly(self):
        content = (_SCRIPTS_DIR / "start-voicevox.sh").read_text()

        assert 'source "$SCRIPT_DIR/lib/readiness.sh"' in content
        assert "wait_for_http" in content
        assert "_wait_for_voicevox()" not in content
        assert "VOICEVOX_HTTP_MAX_ATTEMPTS" not in content

    def test_starts_existing_container_and_waits_for_version_endpoint(self, tmp_path):
        script = self._copy_script(tmp_path)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker_log = tmp_path / "docker.log"
        curl_log = tmp_path / "curl.log"
        docker = bin_dir / "docker"
        docker.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> \"{docker_log}\"\n"
        )
        docker.chmod(0o755)
        curl = bin_dir / "curl"
        curl.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"${{@: -1}}\" >> \"{curl_log}\"\n"
        )
        curl.chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(script)],
            cwd=tmp_path.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert docker_log.read_text().splitlines() == [
            "container inspect voicevox_engine",
            "start voicevox_engine",
        ]
        assert curl_log.read_text().splitlines() == ["http://localhost:50021/version"]

    def test_empty_backend_env_voicevox_base_url_uses_default_container(self, tmp_path):
        script = self._copy_script(tmp_path)
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()
        (backend_dir / ".env").write_text("VOICEVOX_BASE_URL=\n")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker_log = tmp_path / "docker.log"
        curl_log = tmp_path / "curl.log"
        docker = bin_dir / "docker"
        docker.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> \"{docker_log}\"\n"
        )
        docker.chmod(0o755)
        curl = bin_dir / "curl"
        curl.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"${{@: -1}}\" >> \"{curl_log}\"\n"
        )
        curl.chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert docker_log.read_text().splitlines() == [
            "container inspect voicevox_engine",
            "start voicevox_engine",
        ]
        assert curl_log.read_text().splitlines() == ["http://localhost:50021/version"]

    def test_localhost_alias_voicevox_base_url_starts_default_container(self, tmp_path):
        script = self._copy_script(tmp_path)
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()
        (backend_dir / ".env").write_text("VOICEVOX_BASE_URL=http://127.0.0.1:50021/\n")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker_log = tmp_path / "docker.log"
        curl_log = tmp_path / "curl.log"
        docker = bin_dir / "docker"
        docker.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> \"{docker_log}\"\n"
        )
        docker.chmod(0o755)
        curl = bin_dir / "curl"
        curl.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"${{@: -1}}\" >> \"{curl_log}\"\n"
        )
        curl.chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert docker_log.read_text().splitlines() == [
            "container inspect voicevox_engine",
            "start voicevox_engine",
        ]
        assert curl_log.read_text().splitlines() == ["http://localhost:50021/version"]

    def test_profile_overrides_backend_env_voicevox_base_url(
        self, tmp_path
    ):
        script = self._copy_script(tmp_path)
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()
        (backend_dir / ".env").write_text(
            "VOICEVOX_BASE_URL=http://voicevox.local:51000/\n"
        )

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker_log = tmp_path / "docker.log"
        curl_log = tmp_path / "curl.log"
        docker = bin_dir / "docker"
        docker.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> \"{docker_log}\"\n"
        )
        docker.chmod(0o755)
        curl = bin_dir / "curl"
        curl.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"${{@: -1}}\" >> \"{curl_log}\"\n"
        )
        curl.chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert docker_log.read_text().splitlines() == [
            "container inspect voicevox_engine",
            "start voicevox_engine",
        ]
        assert curl_log.read_text().splitlines() == ["http://localhost:50021/version"]

    def test_reports_docker_install_requirement_when_docker_command_is_missing(self, tmp_path):
        script = self._copy_script(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "dirname").symlink_to("/usr/bin/dirname")
        (bin_dir / "python3").symlink_to("/usr/bin/python3")

        env = {**os.environ, "PATH": str(bin_dir)}

        result = subprocess.run(
            ["/bin/bash", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 1
        assert "docker is required to start VOICEVOX" in result.stderr
        assert "VOICEVOX container \"voicevox_engine\" does not exist" not in result.stderr
        assert "docker run -d --name voicevox_engine" in result.stderr

    def test_reports_setup_command_when_container_is_missing(self, tmp_path):
        script = self._copy_script(tmp_path)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker_log = tmp_path / "docker.log"
        docker = bin_dir / "docker"
        docker.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> \"{docker_log}\"\n"
            "case \"$*\" in\n"
            "  \"container inspect voicevox_engine\") "
            "echo 'Error: No such object: voicevox_engine' >&2; exit 1 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        )
        docker.chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 1
        assert 'VOICEVOX container "voicevox_engine" does not exist' in result.stderr
        assert "docker run -d --name voicevox_engine" in result.stderr
        assert docker_log.read_text().splitlines() == [
            "container inspect voicevox_engine",
        ]

    def test_reports_setup_command_for_docker_no_such_container_message(self, tmp_path):
        script = self._copy_script(tmp_path)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  \"container inspect voicevox_engine\") "
            "echo 'Error response from daemon: No such container: voicevox_engine' >&2; exit 1 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        )
        docker.chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 1
        assert 'VOICEVOX container "voicevox_engine" does not exist' in result.stderr
        assert "docker run -d --name voicevox_engine" in result.stderr

    def test_reports_inspect_failure_without_setup_message(self, tmp_path):
        script = self._copy_script(tmp_path)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker_log = tmp_path / "docker.log"
        docker = bin_dir / "docker"
        docker.write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> \"{docker_log}\"\n"
            "case \"$*\" in\n"
            "  \"container inspect voicevox_engine\") "
            "echo 'Cannot connect to the Docker daemon' >&2; exit 1 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
        )
        docker.chmod(0o755)

        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

        result = subprocess.run(
            [str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 1
        assert "failed to inspect VOICEVOX container" in result.stderr
        assert "Cannot connect to the Docker daemon" in result.stderr
        assert "docker run -d --name voicevox_engine" not in result.stderr
        assert docker_log.read_text().splitlines() == [
            "container inspect voicevox_engine",
        ]


def _make_voice_chat_e2e_stub_env(tmp_path: Path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    script = scripts_dir / "start-voice-chat-e2e.sh"
    script.write_text((_SCRIPTS_DIR / "start-voice-chat-e2e.sh").read_text())
    script.chmod(0o755)

    lib_dir = scripts_dir / "lib"
    lib_dir.mkdir()
    for name in _SCRIPT_LIBRARIES:
        (scripts_dir / name).write_text((_SCRIPTS_DIR / name).read_text())
    shutil.copytree(_ROOT_DIR / "environments", tmp_path / "environments")

    voicevox_script = scripts_dir / "start-voicevox.sh"
    voicevox_script.write_text((_SCRIPTS_DIR / "start-voicevox.sh").read_text())
    voicevox_script.chmod(0o755)

    for name in [
        "start-ollama.sh",
        "setup-backend.sh",
        "start-backend.sh",
        "start-frontend.sh",
    ]:
        stub = scripts_dir / name
        stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        stub.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "curl").chmod(0o755)
    (bin_dir / "docker").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "docker").chmod(0o755)

    return scripts_dir, bin_dir


class TestStartVoiceChatE2E:
    def test_uses_shared_process_and_readiness_libraries(self):
        content = (_SCRIPTS_DIR / "start-voice-chat-e2e.sh").read_text()

        assert 'source "$SCRIPT_DIR/lib/process.sh"' in content
        assert 'source "$SCRIPT_DIR/lib/readiness.sh"' in content
        assert "process_manager_init" in content
        assert 'source "$SCRIPT_DIR/lib/profile.sh"' in content
        assert 'profile_resolve "integration-voice"' in content
        assert "profile_start_stack" in content

    def test_does_not_keep_local_process_or_readiness_implementations(self):
        content = (_SCRIPTS_DIR / "start-voice-chat-e2e.sh").read_text()

        for obsolete in [
            "CHILD_PIDS=",
            "_CLEANED_UP=",
            "cleanup()",
            "handle_signal()",
            "_start_child()",
            "_wait_for_children()",
            "_wait_for_http()",
        ]:
            assert obsolete not in content

    def test_calls_voicevox_directly_without_e2e_attempt_override(self):
        content = (_SCRIPTS_DIR / "lib/profile.sh").read_text()

        assert '"$PROFILE_SCRIPTS_DIR/start-voicevox.sh"' in content
        assert "VOICEVOX_HTTP_MAX_ATTEMPTS" not in content
        assert "VOICE_CHAT_E2E_HTTP_MAX_ATTEMPTS" not in content

    def test_real_mode_does_not_pass_legacy_attempt_settings_to_voicevox(self, tmp_path):
        scripts_dir, bin_dir = _make_voice_chat_e2e_stub_env(tmp_path)
        voicevox_env_log = tmp_path / "voicevox-env.log"
        (scripts_dir / "start-ollama.sh").write_text(
            "#!/usr/bin/env bash\nexec sleep 0.2\n"
        )
        (scripts_dir / "start-ollama.sh").chmod(0o755)
        (scripts_dir / "start-backend.sh").write_text(
            "#!/usr/bin/env bash\nexec sleep 0.2\n"
        )
        (scripts_dir / "start-backend.sh").chmod(0o755)
        (scripts_dir / "start-voicevox.sh").write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"${{VOICEVOX_HTTP_MAX_ATTEMPTS-unset}}\" > \"{voicevox_env_log}\"\n"
        )
        (scripts_dir / "start-voicevox.sh").chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VOICE_CHAT_E2E_BACKEND": "real",
            "VOICE_CHAT_E2E_HTTP_MAX_ATTEMPTS": "1",
        }

        result = subprocess.run(
            [str(scripts_dir / "start-voice-chat-e2e.sh")],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert voicevox_env_log.read_text().splitlines() == ["unset"]

    def test_startup_boundary_does_not_source_chat_backend_helper(self):
        content = (_SCRIPTS_DIR / "start-voice-chat-e2e.sh").read_text()

        assert "chat-e2e-backend.sh" not in content

    def test_mock_mode_starts_frontend_only_for_mixed_runs(self, tmp_path):
        scripts_dir, bin_dir = _make_voice_chat_e2e_stub_env(tmp_path)
        order_log = tmp_path / "order.log"
        (scripts_dir / "start-frontend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo start-frontend.sh >> \"{order_log}\"\n"
        )
        (scripts_dir / "start-frontend.sh").chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VOICE_CHAT_E2E_BACKEND": "mock",
        }

        result = subprocess.run(
            [str(scripts_dir / "start-voice-chat-e2e.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert order_log.read_text().splitlines() == ["start-frontend.sh"]

    def test_mixed_legacy_backend_modes_are_rejected_before_frontend(self, tmp_path):
        scripts_dir, bin_dir = _make_voice_chat_e2e_stub_env(tmp_path)
        vite_backend_origin_log = tmp_path / "vite-backend-origin.log"
        (scripts_dir / "start-frontend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"${{VITE_BACKEND_ORIGIN:-}}\" > \"{vite_backend_origin_log}\"\n"
        )
        (scripts_dir / "start-frontend.sh").chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VOICE_CHAT_E2E_BACKEND": "mock",
            "CHAT_E2E_BACKEND": "real",
            "CHAT_E2E_BACKEND_ORIGIN": "http://127.0.0.1:18000",
        }

        result = subprocess.run(
            [str(scripts_dir / "start-voice-chat-e2e.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode != 0
        assert not vite_backend_origin_log.exists()

    def test_default_real_mode_fails_when_backend_health_check_fails(self, tmp_path):
        scripts_dir, bin_dir = _make_voice_chat_e2e_stub_env(tmp_path)
        order_log = tmp_path / "order.log"
        frontend_marker = tmp_path / "frontend-started"

        (scripts_dir / "start-ollama.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo start-ollama.sh >> \"{order_log}\"\n"
            f"exec sleep 30\n"
        )
        (scripts_dir / "start-ollama.sh").chmod(0o755)
        (scripts_dir / "setup-backend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo setup-backend.sh >> \"{order_log}\"\n"
        )
        (scripts_dir / "setup-backend.sh").chmod(0o755)
        (scripts_dir / "start-backend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo start-backend.sh >> \"{order_log}\"\n"
            f"exit 1\n"
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
            "  *localhost:50021*) exit 0 ;;\n"
            f"  *localhost:8000*)\n"
            f"    for _ in {{1..100}}; do\n"
            f"      grep -Fxq start-backend.sh \"{order_log}\" 2>/dev/null && exit 1\n"
            f"      sleep 0.01\n"
            f"    done\n"
            f"    exit 1\n"
            f"    ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        (bin_dir / "curl").chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HTTP_READINESS_MAX_ATTEMPTS": "1",
            "HTTP_READINESS_INTERVAL_SECONDS": "0",
        }

        result = subprocess.run(
            [str(scripts_dir / "start-voice-chat-e2e.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 1
        assert "Backend process exited before becoming ready" in result.stderr
        assert order_log.read_text().splitlines() == [
            "setup-backend.sh",
            "start-ollama.sh",
            "start-backend.sh",
        ]
        assert not frontend_marker.exists()

    def test_mock_mode_starts_frontend_only(self, tmp_path):
        scripts_dir, bin_dir = _make_voice_chat_e2e_stub_env(tmp_path)
        order_log = tmp_path / "order.log"

        for name in ["start-ollama.sh", "setup-backend.sh", "start-backend.sh"]:
            (scripts_dir / name).write_text(
                f"#!/usr/bin/env bash\n"
                f"echo {name} >> \"{order_log}\"\n"
            )
            (scripts_dir / name).chmod(0o755)
        (scripts_dir / "start-frontend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo start-frontend.sh >> \"{order_log}\"\n"
        )
        (scripts_dir / "start-frontend.sh").chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VOICE_CHAT_E2E_BACKEND": "mock",
        }

        result = subprocess.run(
            [str(scripts_dir / "start-voice-chat-e2e.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert order_log.read_text().splitlines() == ["start-frontend.sh"]

    def test_real_mode_backend_health_check_failure_stops_before_frontend(self, tmp_path):
        scripts_dir, bin_dir = _make_voice_chat_e2e_stub_env(tmp_path)
        order_log = tmp_path / "order.log"
        frontend_marker = tmp_path / "frontend-started"

        (scripts_dir / "start-ollama.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo start-ollama.sh >> \"{order_log}\"\n"
            f"exec sleep 30\n"
        )
        (scripts_dir / "start-ollama.sh").chmod(0o755)
        (scripts_dir / "setup-backend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo setup-backend.sh >> \"{order_log}\"\n"
        )
        (scripts_dir / "setup-backend.sh").chmod(0o755)
        (scripts_dir / "start-backend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo start-backend.sh >> \"{order_log}\"\n"
            f"exit 1\n"
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
            "  *localhost:50021*) exit 0 ;;\n"
            f"  *localhost:8000*)\n"
            f"    for _ in {{1..100}}; do\n"
            f"      grep -Fxq start-backend.sh \"{order_log}\" 2>/dev/null && exit 1\n"
            f"      sleep 0.01\n"
            f"    done\n"
            f"    exit 1\n"
            f"    ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        (bin_dir / "curl").chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VOICE_CHAT_E2E_BACKEND": "real",
            "HTTP_READINESS_MAX_ATTEMPTS": "1",
            "HTTP_READINESS_INTERVAL_SECONDS": "0",
        }

        result = subprocess.run(
            [str(scripts_dir / "start-voice-chat-e2e.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode != 0
        assert order_log.read_text().splitlines() == [
            "setup-backend.sh",
            "start-ollama.sh",
            "start-backend.sh",
        ]
        assert not frontend_marker.exists()

    def test_real_mode_fails_when_voicevox_health_check_fails(self, tmp_path):
        scripts_dir, bin_dir = _make_voice_chat_e2e_stub_env(tmp_path)
        order_log = tmp_path / "order.log"
        frontend_marker = tmp_path / "frontend-started"

        (scripts_dir / "start-ollama.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo start-ollama.sh >> \"{order_log}\"\n"
            f"exec sleep 30\n"
        )
        (scripts_dir / "start-ollama.sh").chmod(0o755)
        (scripts_dir / "setup-backend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo setup-backend.sh >> \"{order_log}\"\n"
        )
        (scripts_dir / "setup-backend.sh").chmod(0o755)
        (scripts_dir / "start-backend.sh").write_text(
            f"#!/usr/bin/env bash\n"
            f"echo start-backend.sh >> \"{order_log}\"\n"
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
            "  *localhost:50021*) exit 1 ;;\n"
            "  *localhost:8000*) exit 0 ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        (bin_dir / "curl").chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VOICE_CHAT_E2E_BACKEND": "real",
            "HTTP_READINESS_MAX_ATTEMPTS": "1",
            "HTTP_READINESS_INTERVAL_SECONDS": "0",
        }

        result = subprocess.run(
            [str(scripts_dir / "start-voice-chat-e2e.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode != 0
        assert "VOICEVOX did not become ready" in result.stderr
        assert order_log.read_text().splitlines() == [
            "setup-backend.sh",
            "start-ollama.sh",
        ]
        assert not frontend_marker.exists()

    def test_auto_mode_is_rejected(self, tmp_path):
        scripts_dir, bin_dir = _make_voice_chat_e2e_stub_env(tmp_path)

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VOICE_CHAT_E2E_BACKEND": "auto",
        }

        result = subprocess.run(
            [str(scripts_dir / "start-voice-chat-e2e.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode != 0
        assert 'VOICE_CHAT_E2E_BACKEND must be "mock" or "real"' in result.stderr


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
