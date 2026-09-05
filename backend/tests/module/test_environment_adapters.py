from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from adapters.base import AdapterOperationError, Check, OperationContext

from tests.environment_test_support import (
    RecordingRunner,
    resolved_profile,
    resolved_runtime_paths,
    write_cached_whisper_model,
)


def test_should_import_concrete_adapters_with_backend_package_contract():
    from adapters.backend import BackendAdapter
    from adapters.ollama import OllamaAdapter

    assert BackendAdapter.__name__ == "BackendAdapter"
    assert OllamaAdapter.__name__ == "OllamaAdapter"


ROOT_DIR = Path(__file__).parent.parent.parent.parent
OPERATION_CONTEXT = OperationContext(whisper_enabled=False, chroma_enabled=False)


def _available_local_port() -> int:
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _frontend_dependency(port: int) -> dict[str, object]:
    return {
        "mode": "real",
        "source": "managed",
        "baseUrl": f"http://127.0.0.1:{port}",
        "readinessPath": "/",
        "readinessUrl": f"http://127.0.0.1:{port}/",
        "host": "127.0.0.1",
        "port": port,
    }


def _dogfood_frontend_adapter(root_dir: Path):
    from service_registry import create_service_registry, require_service_operations

    registry = create_service_registry(
        root_dir,
        resolved_runtime_paths(root_dir),
        effective_profile="dogfood",
    )
    return require_service_operations(registry, "frontend")


def _wait_for_frontend(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"frontend exited before readiness\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            with urlopen(url, timeout=0.2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("frontend did not become ready")


def _write_built_frontend_fixture(tmp_path: Path) -> Path:
    frontend = tmp_path / "frontend"
    dist = frontend / "dist"
    dist.mkdir(parents=True)
    frontend.joinpath("built-frontend-server.mjs").write_bytes(
        ROOT_DIR.joinpath("frontend", "built-frontend-server.mjs").read_bytes()
    )
    dist.joinpath("index.html").write_text(
        "<!doctype html><title>dogfood</title>", encoding="utf-8"
    )
    return frontend


def _start_built_frontend(
    frontend: Path, port: int, backend_port: int
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        (
            "node",
            str(frontend / "built-frontend-server.mjs"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ),
        cwd=frontend,
        env={
            **os.environ,
            "DS_BACKEND_ORIGIN": f"http://127.0.0.1:{backend_port}",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, 15)
        process.wait(timeout=5)


def _receive_until(connection: socket.socket, marker: bytes) -> tuple[bytes, bytes]:
    received = b""
    while marker not in received:
        chunk = connection.recv(4096)
        if not chunk:
            raise AssertionError("connection closed before complete message")
        received += chunk
    before, after = received.split(marker, 1)
    return before + marker, after


def _receive_exact(
    connection: socket.socket, size: int, initial: bytes = b""
) -> tuple[bytes, bytes]:
    received = initial
    while len(received) < size:
        chunk = connection.recv(4096)
        if not chunk:
            raise AssertionError("connection closed before complete payload")
        received += chunk
    return received[:size], received[size:]


def _receive_websocket_text(
    connection: socket.socket, initial: bytes = b""
) -> tuple[str, bytes]:
    header, remaining = _receive_exact(connection, 2, initial)
    payload_length = header[1] & 0x7F
    assert payload_length < 126
    masked = (header[1] & 0x80) != 0
    mask = b""
    if masked:
        mask, remaining = _receive_exact(connection, 4, remaining)
    payload, remaining = _receive_exact(connection, payload_length, remaining)
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return payload.decode("utf-8"), remaining


def _client_websocket_frame(payload: str) -> bytes:
    encoded = payload.encode("utf-8")
    assert len(encoded) < 126
    mask = b"test"
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(encoded))
    return bytes((0x81, 0x80 | len(encoded))) + mask + masked


class _ProxyBackendFixture:
    def __init__(self) -> None:
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port = int(self.listener.getsockname()[1])
        self.http_target: str | None = None
        self.websocket_target: str | None = None
        self.websocket_payload: str | None = None
        self.websocket_received = threading.Event()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                connection, _address = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self.stopped.is_set():
                    return
                raise
            with connection:
                request, _remaining = _receive_until(connection, b"\r\n\r\n")
                lines = request.decode("latin-1").split("\r\n")
                target = lines[0].split(" ")[1]
                headers = {
                    key.lower(): value.strip()
                    for line in lines[1:]
                    if ":" in line
                    for key, value in (line.split(":", 1),)
                }
                if headers.get("upgrade", "").lower() == "websocket":
                    self.websocket_target = target
                    accept = base64.b64encode(
                        hashlib.sha1(
                            (
                                headers["sec-websocket-key"]
                                + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                            ).encode()
                        ).digest()
                    ).decode()
                    connection.sendall(
                        (
                            "HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                        ).encode()
                        + bytes((0x81, len("from-backend")))
                        + b"from-backend"
                    )
                    self.websocket_payload, _remaining = _receive_websocket_text(
                        connection
                    )
                    self.websocket_received.set()
                    continue
                self.http_target = target
                body = target.encode()
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    + (
                        f"Content-Length: {len(body)}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                    + body
                )

    def close(self) -> None:
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=2)


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


def test_should_delegate_frontend_preparation_to_the_container_image(
    tmp_path: Path,
) -> None:
    from adapters.frontend import FrontendAdapter

    runner = RecordingRunner()
    adapter = FrontendAdapter(
        tmp_path, resolved_runtime_paths(tmp_path), runner
    )

    adapter.prepare(
        resolved_profile()["dependencies"]["frontend"], OPERATION_CONTEXT
    )

    assert runner.calls == []


def test_should_verify_frontend_through_docker_compose(tmp_path: Path) -> None:
    from adapters.frontend import FrontendAdapter

    runner = RecordingRunner()
    result = FrontendAdapter(
        tmp_path, resolved_runtime_paths(tmp_path), runner
    ).verify(_frontend_dependency(5173), OPERATION_CONTEXT)

    assert runner.calls == [("docker", "compose", "version")]
    assert result.checks == (
        Check("docker-compose", "ready", "Docker Engine and Compose plugin", False),
    )


def test_should_serve_built_frontend_without_writing_to_read_only_clone(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    frontend = clone / "frontend"
    dist = frontend / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>dogfood</title>", encoding="utf-8")
    for asset in ROOT_DIR.joinpath("frontend").glob("*.mjs"):
        frontend.joinpath(asset.name).write_bytes(asset.read_bytes())
    (frontend / "node_modules").symlink_to(ROOT_DIR / "frontend" / "node_modules")
    subprocess.run(("git", "init", "--quiet", str(clone)), check=True)
    subprocess.run(("git", "-C", str(clone), "add", "."), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(clone),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ),
        check=True,
    )
    status_before = subprocess.run(
        ("git", "-C", str(clone), "status", "--porcelain"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for path in sorted(clone.rglob("*"), reverse=True):
        if ".git" not in path.parts and not path.is_symlink():
            path.chmod(0o550 if path.is_dir() else 0o440)
    frontend.chmod(0o550)
    clone.chmod(0o550)
    runtime = tmp_path / "runtime"
    cache = runtime / "cache"
    npm_cache = cache / "npm"
    home = runtime / "home"
    for path in (cache, npm_cache, home):
        path.mkdir(parents=True, exist_ok=True)
    port = _available_local_port()
    process = subprocess.Popen(
        (
            "node",
            str(frontend / "built-frontend-server.mjs"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ),
        cwd=clone,
        env={
            **os.environ,
            "HOME": str(home),
            "XDG_CACHE_HOME": str(cache),
            "npm_config_cache": str(npm_cache),
            "DS_BACKEND_ORIGIN": "http://127.0.0.1:18000",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_for_frontend(f"http://127.0.0.1:{port}/", process)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 15)
            process.wait(timeout=5)
        clone.chmod(0o750)
        frontend.chmod(0o750)
        for path in clone.rglob("*"):
            if ".git" not in path.parts and not path.is_symlink():
                path.chmod(0o750 if path.is_dir() else 0o640)

    status_after = subprocess.run(
        ("git", "-C", str(clone), "status", "--porcelain"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    runtime_files = tuple(
        path.relative_to(clone)
        for path in clone.rglob("*")
        if path.name.startswith(".timestamp-") and path.suffix == ".mjs"
    )
    assert status_before == ""
    assert status_after == ""
    assert runtime_files == ()


@pytest.mark.parametrize("path", ("/%00", "/%00/foo"))
def test_should_reject_nul_static_path_and_continue_serving(
    tmp_path: Path, path: str,
) -> None:
    frontend = _write_built_frontend_fixture(tmp_path)
    frontend_port = _available_local_port()
    process = _start_built_frontend(
        frontend, frontend_port, _available_local_port()
    )
    try:
        _wait_for_frontend(f"http://127.0.0.1:{frontend_port}/", process)

        with pytest.raises(HTTPError) as rejected:
            urlopen(f"http://127.0.0.1:{frontend_port}{path}", timeout=1)

        assert rejected.value.code == 400
        with urlopen(f"http://127.0.0.1:{frontend_port}/", timeout=1) as response:
            assert response.status == 200
    finally:
        _stop_process(process)


def test_should_report_frontend_listen_failure(tmp_path: Path) -> None:
    frontend = _write_built_frontend_fixture(tmp_path)
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        process = _start_built_frontend(frontend, port, _available_local_port())
        _stdout, stderr = process.communicate(timeout=5)

    assert process.returncode != 0
    assert "frontend server failed" in stderr
    assert "EADDRINUSE" in stderr


def test_should_proxy_http_and_websocket_to_backend(tmp_path: Path) -> None:
    frontend = _write_built_frontend_fixture(tmp_path)
    backend = _ProxyBackendFixture()
    process = None
    try:
        frontend_port = _available_local_port()
        process = _start_built_frontend(frontend, frontend_port, backend.port)
        _wait_for_frontend(f"http://127.0.0.1:{frontend_port}/", process)

        with urlopen(
            f"http://127.0.0.1:{frontend_port}/api/chat?x=1", timeout=1
        ) as response:
            assert response.read() == b"/chat?x=1"

        client_socket = socket.socket()
        client_socket.settimeout(1)
        client_socket.connect(("127.0.0.1", frontend_port))
        with closing(client_socket) as client:
            client.sendall(
                b"GET /ws/channel?x=1 HTTP/1.1\r\n"
                + f"Host: 127.0.0.1:{frontend_port}\r\n".encode()
                + b"Upgrade: websocket\r\n"
                + b"Connection: Upgrade\r\n"
                + b"Sec-WebSocket-Version: 13\r\n"
                + b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
            )
            handshake, remaining = _receive_until(client, b"\r\n\r\n")
            assert handshake.startswith(b"HTTP/1.1 101 ")
            backend_payload, _remaining = _receive_websocket_text(client, remaining)
            assert backend_payload == "from-backend"
            client.sendall(_client_websocket_frame("from-frontend"))
            assert backend.websocket_received.wait(timeout=1)

        assert backend.http_target == "/chat?x=1"
        assert backend.websocket_target == "/ws/channel?x=1"
        assert backend.websocket_payload == "from-frontend"
    finally:
        try:
            if process is not None:
                _stop_process(process)
        finally:
            backend.close()


def test_should_preserve_container_identity_mismatch_without_removing_it(
    tmp_path: Path,
) -> None:
    from adapters.frontend import FrontendAdapter
    runner = RecordingRunner(
        [{
            "returncode": 0,
            "stdout": json.dumps([{
                "Id": "b" * 64,
                "State": {"StartedAt": "2026-08-30T01:00:00Z", "Running": True},
            }]),
            "stderr": "",
        }]
    )
    adapter = FrontendAdapter(
        tmp_path, resolved_runtime_paths(tmp_path), runner
    )
    service = {
        "containerIdentity": {
            "containerId": "a" * 64,
            "startedAt": "2026-08-30T00:00:00Z",
        }
    }

    result = adapter.stop(service, grace_seconds=5.0)

    assert result.result == "skipped_identity_mismatch"
    assert runner.calls == [
        ("docker", "inspect", "digital-souls-test-frontend")
    ]


def test_should_require_the_recorded_container_start_time(tmp_path: Path) -> None:
    from adapters.frontend import FrontendAdapter
    runner = RecordingRunner(
        [{
            "returncode": 0,
            "stdout": json.dumps([{
                "Id": "a" * 64,
                "State": {"StartedAt": "2026-08-30T01:00:00Z", "Running": True},
            }]),
            "stderr": "",
        }]
    )
    adapter = FrontendAdapter(
        tmp_path, resolved_runtime_paths(tmp_path), runner
    )

    result = adapter.stop(
        {
            "containerIdentity": {
                "containerId": "a" * 64,
                "startedAt": "2026-08-30T00:00:00Z",
            }
        },
        grace_seconds=5.0,
    )

    assert result.result == "skipped_identity_mismatch"


def test_should_remove_owned_container_after_it_has_exited(tmp_path: Path) -> None:
    from adapters.frontend import FrontendAdapter

    identity = {
        "containerId": "a" * 64,
        "startedAt": "2026-08-30T00:00:00Z",
    }
    runner = RecordingRunner(
        [
            {
                "returncode": 0,
                "stdout": json.dumps(
                    [
                        {
                            "Id": identity["containerId"],
                            "State": {
                                "StartedAt": identity["startedAt"],
                                "Running": False,
                            },
                        }
                    ]
                ),
                "stderr": "",
            },
            {"returncode": 0, "stdout": identity["containerId"], "stderr": ""},
        ]
    )
    adapter = FrontendAdapter(tmp_path, resolved_runtime_paths(tmp_path), runner)

    result = adapter.stop({"containerIdentity": identity}, grace_seconds=5.0)

    assert result.result == "stopped"
    assert runner.calls == [
        ("docker", "inspect", "digital-souls-test-frontend"),
        ("docker", "rm", "--force", identity["containerId"]),
    ]


def test_should_apply_dogfood_compose_overlay_to_managed_services(
    tmp_path: Path,
) -> None:
    from adapters.backend import BackendAdapter

    adapter = BackendAdapter(
        tmp_path,
        resolved_runtime_paths(tmp_path),
        RecordingRunner(),
        effective_profile="dogfood",
    )

    command = adapter._compose_command(  # noqa: SLF001
        {"DS_CONTAINER_ENV_FILE": "/run/digital-souls/backend.env"}
    )

    assert command == (
        "docker",
        "compose",
        "--env-file",
        "/run/digital-souls/backend.env",
        "--file",
        str(tmp_path / "infra" / "application" / "compose.yaml"),
        "--file",
        str(tmp_path / "infra" / "application" / "compose.dogfood.yaml"),
    )


def test_should_stage_dogfood_backup_directory_for_frontend_compose(
    tmp_path: Path,
) -> None:
    from adapters.frontend import FrontendAdapter

    profile_report = tmp_path / "resolved-profile.json"
    profile_report.write_text("{}", encoding="utf-8")
    backup_directory = tmp_path / "backups"
    adapter = FrontendAdapter(
        tmp_path,
        resolved_runtime_paths(tmp_path),
        RecordingRunner(),
        effective_profile="dogfood",
    )

    values = adapter._write_compose_environment(  # noqa: SLF001
        _frontend_dependency(15173),
        {
            "DS_RUNTIME_UID": "10001",
            "DS_RUNTIME_GID": "10001",
            "DS_PROFILE_REPORT": str(profile_report),
            "DOGFOOD_FRONTEND_IMAGE": (
                "ghcr.io/example/frontend@sha256:" + "a" * 64
            ),
            "DOGFOOD_CONFIG_DIR": str(tmp_path / "config"),
            "DOGFOOD_BACKUP_DIR": str(backup_directory),
        },
        host="127.0.0.1",
        port=15173,
    )

    assert values["DOGFOOD_BACKUP_DIR"] == str(backup_directory)


def test_should_prepare_backend_data_without_host_toolchain(tmp_path: Path):
    from adapters.backend import BackendAdapter

    runner = RecordingRunner()
    adapter = BackendAdapter(
        root_dir=tmp_path, runtime_paths=resolved_runtime_paths(tmp_path), runner=runner
    )
    dependency = resolved_profile()["dependencies"]["backend"]

    adapter.prepare(dependency, OPERATION_CONTEXT)

    assert runner.calls == []
    assert resolved_runtime_paths(tmp_path).identity_marker_path.is_file()


def test_should_not_assign_shared_whisper_preparation_to_backend(tmp_path: Path):
    from adapters.backend import BackendAdapter

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
    ).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert all(not check.name.startswith("whisper-model-") for check in result.checks)


def test_should_leave_shared_whisper_cache_outside_backend_prepare(tmp_path: Path):
    from adapters.backend import BackendAdapter

    runner = RecordingRunner()
    runtime_paths = resolved_runtime_paths(tmp_path)
    adapter = BackendAdapter(
        root_dir=tmp_path, runtime_paths=runtime_paths, runner=runner
    )

    adapter.prepare(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert runner.calls == []


def test_should_not_run_whisper_inference_in_backend_prepare(tmp_path: Path):
    from adapters.backend import BackendAdapter

    snapshot = write_cached_whisper_model(
        tmp_path, "Systran/faster-whisper-medium"
    )
    runner = RecordingRunner(
        [
            {"returncode": 0, "stdout": "", "stderr": ""},
            {"returncode": 0, "stdout": f"{snapshot}\n", "stderr": ""},
            {"returncode": 0, "stdout": "", "stderr": ""},
        ]
    )
    runtime_paths = resolved_runtime_paths(tmp_path)
    adapter = BackendAdapter(tmp_path, runtime_paths, runner)

    adapter.prepare(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert runner.calls == []


def test_should_ignore_host_whisper_state_during_backend_prepare(
    tmp_path: Path,
) -> None:
    from adapters.backend import BackendAdapter

    snapshot = write_cached_whisper_model(
        tmp_path, "Systran/faster-whisper-medium"
    )
    missing_library = "Library libcublas.so.12 is not found or cannot be loaded"
    runner = RecordingRunner(
        [
            {"returncode": 0, "stdout": "", "stderr": ""},
            {"returncode": 0, "stdout": f"{snapshot}\n", "stderr": ""},
            {"returncode": 1, "stdout": "", "stderr": missing_library},
        ]
    )
    adapter = BackendAdapter(tmp_path, resolved_runtime_paths(tmp_path), runner)

    adapter.prepare(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert runner.calls == []


def test_should_not_expose_host_whisper_cache_as_backend_check(tmp_path: Path):
    from adapters.backend import BackendAdapter

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
    ).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert all(not check.name.startswith("whisper-model-") for check in result.checks)


@pytest.mark.parametrize(
    ("model_name", "repository_id"),
    [
        ("medium", "Systran/faster-whisper-medium"),
        ("large", "Systran/faster-whisper-large-v3"),
        ("distil-large-v3", "Systran/faster-distil-whisper-large-v3"),
        ("turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
        ("example/converted-whisper", "example/converted-whisper"),
    ],
)
def test_should_verify_cache_resolved_by_faster_whisper(
    tmp_path: Path, model_name: str, repository_id: str
) -> None:
    from adapters.backend import BackendAdapter

    write_cached_whisper_model(tmp_path, repository_id)

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
        whisper_model_name=model_name,
    ).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert all(not check.name.startswith("whisper-model-") for check in result.checks)


def test_should_ignore_empty_host_whisper_cache(tmp_path: Path):
    from adapters.backend import BackendAdapter

    write_cached_whisper_model(
        tmp_path, "Systran/faster-whisper-medium", complete=False
    )

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
    ).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert all(not check.name.startswith("whisper-model-") for check in result.checks)


def test_should_require_docker_compose_instead_of_host_backend_launchers(
    tmp_path: Path,
) -> None:
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

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
    ).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=False, chroma_enabled=False),
    )

    assert result.checks == (
        Check("docker-compose", "ready", "Docker Engine and Compose plugin", False),
    )


def test_should_prepare_chroma_directory_only_in_prepare(tmp_path: Path):
    from adapters.backend import BackendAdapter

    chroma_path = resolved_runtime_paths(tmp_path).chroma_path
    adapter = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
    )

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


def test_should_converge_dogfood_chroma_directory_to_runtime_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import adapters.backend
    from adapters.backend import BackendAdapter

    runtime_paths = resolved_runtime_paths(tmp_path)
    chown_calls: list[tuple[Path, int, int]] = []
    monkeypatch.setenv("DS_RUNTIME_UID", "997")
    monkeypatch.setenv("DS_RUNTIME_GID", "986")
    monkeypatch.setattr(
        adapters.backend.os,
        "chown",
        lambda path, uid, gid: chown_calls.append((path, uid, gid)),
    )

    BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=runtime_paths,
        runner=RecordingRunner(),
        effective_profile="dogfood",
    ).prepare(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=False, chroma_enabled=True),
    )

    assert chown_calls == [(runtime_paths.chroma_path, 997, 986)]
    assert runtime_paths.chroma_path.stat().st_mode & 0o777 == 0o750


def test_should_reject_missing_dogfood_runtime_identity_before_chroma_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from adapters.backend import BackendAdapter

    runtime_paths = resolved_runtime_paths(tmp_path)
    monkeypatch.delenv("DS_RUNTIME_UID", raising=False)
    monkeypatch.setenv("DS_RUNTIME_GID", "986")

    with pytest.raises(AdapterOperationError, match="DS_RUNTIME_UID"):
        BackendAdapter(
            root_dir=tmp_path,
            runtime_paths=runtime_paths,
            runner=RecordingRunner(),
            effective_profile="dogfood",
        ).prepare(
            resolved_profile()["dependencies"]["backend"],
            OperationContext(whisper_enabled=False, chroma_enabled=True),
        )

    assert not runtime_paths.chroma_path.exists()


def test_should_classify_chroma_file_collision_as_not_preparable(tmp_path: Path):
    from adapters.backend import BackendAdapter

    chroma_path = resolved_runtime_paths(tmp_path).chroma_path
    chroma_path.parent.mkdir(parents=True, exist_ok=True)
    chroma_path.write_text("not a directory", encoding="utf-8")

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
    ).verify(
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
    import adapters.backend
    from adapters.backend import BackendAdapter

    chroma_path = resolved_runtime_paths(tmp_path).chroma_path
    chroma_path.mkdir(parents=True)
    real_access = adapters.backend.os.access
    monkeypatch.setattr(
        adapters.backend.os,
        "access",
        lambda path, mode: False if path == chroma_path else real_access(path, mode),
    )

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
    ).verify(
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
    import adapters.ollama
    from adapters.ollama import OllamaAdapter

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


def test_should_guide_missing_effective_ollama_model_to_external_service_pull(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import adapters.ollama
    from adapters.ollama import OllamaAdapter

    effective_model = "custom-chat:model"
    monkeypatch.setattr(
        adapters.ollama,
        "_fetch_json",
        lambda _url: {"models": [{"name": "other:latest"}]},
    )

    result = OllamaAdapter(tmp_path, model_name=effective_model).validate_readiness(
        resolved_profile()["dependencies"]["ollama"]
    )

    assert result.classification == "preparation"
    assert result.message is not None
    assert f"ollama pull {effective_model}" in result.message
    assert "service account, HOME, and OLLAMA_MODELS" in result.message
    assert "digital-souls" not in result.message
    assert "/var/lib" not in result.message


def test_should_quote_effective_ollama_model_in_recovery_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import adapters.ollama
    from adapters.ollama import OllamaAdapter

    effective_model = "custom; touch /tmp/not-a-command"
    monkeypatch.setattr(adapters.ollama, "_fetch_json", lambda _url: {"models": []})

    result = OllamaAdapter(tmp_path, model_name=effective_model).validate_readiness(
        resolved_profile()["dependencies"]["ollama"]
    )

    assert result.message is not None
    recovery_command = result.message.partition("Run: ")[2]
    shell_tokens = shlex.shlex(recovery_command, posix=True, punctuation_chars=True)
    shell_tokens.whitespace_split = True
    assert list(shell_tokens)[-3:] == ["ollama", "pull", effective_model]


def test_should_fail_ollama_preparation_when_model_pull_fails(tmp_path: Path) -> None:
    from adapters.ollama import OllamaAdapter, OllamaPreparationError

    runner = RecordingRunner(
        [{"returncode": 1, "stdout": "", "stderr": "pull failed"}]
    )

    with pytest.raises(OllamaPreparationError, match="pull failed"):
        OllamaAdapter(root_dir=tmp_path, runner=runner).prepare(
            resolved_profile()["dependencies"]["ollama"], OPERATION_CONTEXT
        )

    assert runner.calls == [("ollama", "pull", "gemma4:e4b")]


def test_should_prepare_distinct_chat_and_classifier_models(tmp_path: Path) -> None:
    from adapters.ollama import OllamaAdapter

    runner = RecordingRunner(
        [
            {"returncode": 0, "stdout": "", "stderr": ""},
            {"returncode": 0, "stdout": "", "stderr": ""},
        ]
    )

    OllamaAdapter(
        root_dir=tmp_path,
        runner=runner,
        model_name="chat-only:9b",
        classifier_model_name="classifier-only:4b",
    ).prepare(resolved_profile()["dependencies"]["ollama"], OPERATION_CONTEXT)

    assert runner.calls == [
        ("ollama", "pull", "chat-only:9b"),
        ("ollama", "pull", "classifier-only:4b"),
    ]


def test_should_require_distinct_classifier_model_at_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import adapters.ollama
    from adapters.ollama import OllamaAdapter

    monkeypatch.setattr(
        adapters.ollama,
        "_fetch_json",
        lambda _url: {"models": [{"name": "chat-only:9b"}]},
    )

    result = OllamaAdapter(
        tmp_path,
        model_name="chat-only:9b",
        classifier_model_name="classifier-only:4b",
    ).validate_readiness(resolved_profile()["dependencies"]["ollama"])

    assert result.classification == "preparation"
    assert result.message is not None
    assert "classifier-only:4b" in result.message


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
