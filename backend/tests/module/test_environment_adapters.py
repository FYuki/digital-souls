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
from adapters.base import Check, OperationContext
from tests.environment_test_support import (
    RecordingRunner,
    resolved_profile,
    resolved_runtime_paths,
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


def _write_cached_whisper_model(
    root_dir: Path, repository_id: str, *, complete: bool = True
) -> None:
    python = root_dir / "backend" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    repository_cache = (
        root_dir
        / "runtime-data"
        / "cache"
        / "huggingface"
        / "hub"
        / f"models--{repository_id.replace('/', '--')}"
    )
    snapshot = repository_cache / "snapshots" / "revision"
    python.write_text(
        f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(str(snapshot))}\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    snapshot.mkdir(parents=True)
    refs = repository_cache / "refs"
    refs.mkdir()
    (refs / "main").write_text("revision", encoding="utf-8")
    if complete:
        for artifact in (
            "config.json",
            "model.bin",
            "preprocessor_config.json",
            "tokenizer.json",
            "vocabulary.json",
        ):
            (snapshot / artifact).write_text("fixture", encoding="utf-8")


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

    assert runner.calls == [("npm", "ci", "--prefix", str(frontend))]


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


@pytest.mark.parametrize("effective_profile", ("dev", "test-mocked"))
def test_should_keep_dev_frontend_command_for_non_dogfood_profiles(
    tmp_path: Path, effective_profile: str
) -> None:
    from service_registry import create_service_registry, require_service_operations

    frontend = tmp_path / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    registry = create_service_registry(
        tmp_path,
        resolved_runtime_paths(tmp_path),
        effective_profile=effective_profile,
    )

    specification = require_service_operations(
        registry, "frontend"
    ).start_specification(_frontend_dependency(5173))

    assert specification.command == (
        "npm",
        "run",
        "dev",
        "--prefix",
        str(frontend),
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        "5173",
        "--strictPort",
    )


def test_should_start_dogfood_frontend_from_built_assets_without_npm(
    tmp_path: Path,
) -> None:
    adapter = _dogfood_frontend_adapter(tmp_path)

    specification = adapter.start_specification(_frontend_dependency(15173))

    assert specification.command == (
        "node",
        str(tmp_path / "frontend" / "built-frontend-server.mjs"),
        "--host",
        "127.0.0.1",
        "--port",
        "15173",
    )


def test_should_not_install_or_build_frontend_during_dogfood_prepare(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    from service_registry import create_service_registry, require_service_operations

    registry = create_service_registry(
        tmp_path,
        resolved_runtime_paths(tmp_path),
        runner,
        effective_profile="dogfood",
    )
    adapter = require_service_operations(registry, "frontend")

    adapter.prepare(_frontend_dependency(15173), OPERATION_CONTEXT)

    assert runner.calls == []


def test_should_identify_unreadable_dogfood_frontend_asset_as_eacces(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend"
    dist = frontend / "dist"
    node_modules = frontend / "node_modules"
    dist.mkdir(parents=True)
    node_modules.mkdir()
    launcher = frontend / "built-frontend-server.mjs"
    launcher.write_text("", encoding="utf-8")
    index = dist / "index.html"
    index.write_text("fixture", encoding="utf-8")
    index.chmod(0)
    adapter = _dogfood_frontend_adapter(tmp_path)

    result = adapter.verify(_frontend_dependency(15173), OPERATION_CONTEXT)

    failed = tuple(
        check for check in result.checks if check.classification == "preparation_required"
    )
    assert any("EACCES" in check.message and str(index) in check.message for check in failed)


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
    specification = _dogfood_frontend_adapter(clone).start_specification(
        _frontend_dependency(port)
    )
    process = subprocess.Popen(
        specification.command,
        cwd=specification.cwd,
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


def test_should_preserve_identity_mismatch_skip_without_waiting_or_signaling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from adapters.frontend import FrontendAdapter
    from process_control import ManagedProcess, ProcessIdentity

    class ReusedPidProcess:
        pid = 4102

        def __init__(self) -> None:
            self.wait_calls: list[float] = []

        def poll(self):
            return None

        def wait(self, timeout: float):
            self.wait_calls.append(timeout)
            raise subprocess.TimeoutExpired("unrelated-process", timeout)

    process = ReusedPidProcess()
    identity = ProcessIdentity(pid=4101, pgid=4101, session_id=4101, start_time=99101)
    sent_signals: list[tuple[int, int]] = []
    adapter = FrontendAdapter(root_dir=tmp_path, runner=RecordingRunner())
    adapter._process = ManagedProcess(
        label="frontend",
        process=process,  # type: ignore[arg-type]
        identity=identity,
    )
    monkeypatch.setattr(
        "process_control.os.killpg",
        lambda pgid, sent_signal: sent_signals.append((pgid, sent_signal)),
    )
    service = {"processIdentity": identity.to_report()}

    result = adapter.stop(service, grace_seconds=5.0)

    assert result.result == "skipped_identity_mismatch"
    assert process.wait_calls == []
    assert sent_signals == []


def test_should_refuse_managed_group_with_member_older_than_recorded_leader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from adapters.frontend import FrontendAdapter
    from process_control import ManagedProcess, ProcessIdentity

    class MatchingLeaderProcess:
        pid = 4101

        def __init__(self) -> None:
            self.wait_calls: list[float] = []

        def poll(self):
            return None

        def wait(self, timeout: float):
            self.wait_calls.append(timeout)
            raise subprocess.TimeoutExpired("unrelated-process", timeout)

    process = MatchingLeaderProcess()
    identity = ProcessIdentity(pid=4101, pgid=4101, session_id=4101, start_time=99101)
    sent_signals: list[tuple[int, int]] = []
    adapter = FrontendAdapter(root_dir=tmp_path, runner=RecordingRunner())
    adapter._process = ManagedProcess(
        label="frontend",
        process=process,  # type: ignore[arg-type]
        identity=identity,
    )
    monkeypatch.setattr("process_control._leader_identity_matches", lambda value: True)
    monkeypatch.setattr("process_control._group_members", lambda value: ((4102, 99100),))
    monkeypatch.setattr(
        "process_control.os.killpg",
        lambda pgid, sent_signal: sent_signals.append((pgid, sent_signal)),
    )

    result = adapter.stop(
        {"processIdentity": identity.to_report()}, grace_seconds=5.0
    )

    assert result.result == "skipped_identity_mismatch"
    assert process.wait_calls == []
    assert sent_signals == []


def test_should_keep_backend_setup_in_prepare_and_uvicorn_in_start(tmp_path: Path):
    from adapters.backend import BackendAdapter

    runner = RecordingRunner()
    adapter = BackendAdapter(
        root_dir=tmp_path, runtime_paths=resolved_runtime_paths(tmp_path), runner=runner
    )
    dependency = resolved_profile()["dependencies"]["backend"]

    adapter.prepare(dependency, OPERATION_CONTEXT)
    start = adapter.start_specification(dependency)

    assert runner.calls == [(str(tmp_path / "scripts" / "setup-backend.sh"),)]
    assert start.command == (
        str(tmp_path / "scripts" / "start-backend.sh"),
        "--host",
        "localhost",
        "--port",
        "8000",
        "--reload",
    )


def test_should_classify_missing_whisper_cache_as_preparation_required(tmp_path: Path):
    from adapters.backend import BackendAdapter

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
    ).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    whisper = next(check for check in result.checks if check.name == "whisper-model-medium")
    assert whisper.classification == "preparation_required"


def test_should_prepare_whisper_model_in_cache_used_by_backend_runtime(tmp_path: Path):
    from adapters.backend import BackendAdapter
    from app.model_settings import WHISPER_MODEL_NAME

    runner = RecordingRunner()
    runtime_paths = resolved_runtime_paths(tmp_path)
    adapter = BackendAdapter(
        root_dir=tmp_path, runtime_paths=runtime_paths, runner=runner
    )

    adapter.prepare(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert runner.calls[0] == (str(tmp_path / "scripts" / "setup-backend.sh"),)
    assert runner.calls[1][0] == str(tmp_path / "backend" / ".venv" / "bin" / "python")
    assert runner.calls[1][3] == WHISPER_MODEL_NAME
    assert runner.calls[1][4] == str(runtime_paths.whisper_cache_path)


def test_should_mark_missing_whisper_cache_as_preparable(tmp_path: Path):
    from adapters.backend import BackendAdapter

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
    ).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    whisper = next(check for check in result.checks if check.name == "whisper-model-medium")
    assert whisper.can_prepare is True


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

    _write_cached_whisper_model(tmp_path, repository_id)

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        whisper_model_name=model_name,
    ).verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    whisper = next(
        check for check in result.checks if check.name == f"whisper-model-{model_name}"
    )
    assert whisper.classification == "ready"


def test_should_treat_empty_whisper_cache_as_preparation_required(tmp_path: Path):
    from adapters.backend import BackendAdapter

    _write_cached_whisper_model(
        tmp_path, "Systran/faster-whisper-medium", complete=False
    )

    result = BackendAdapter(
        root_dir=tmp_path, runtime_paths=resolved_runtime_paths(tmp_path)
    ).verify(
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

    result = BackendAdapter(
        root_dir=tmp_path,
        runtime_paths=resolved_runtime_paths(tmp_path),
        runner=RecordingRunner(),
    ).verify(
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
