from __future__ import annotations

from pathlib import Path

from adapters.base import OperationContext
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


def test_should_start_dogfood_frontend_on_resolved_endpoint_with_strict_port(
    tmp_path: Path,
) -> None:
    from adapters.frontend import FrontendAdapter

    dependency = _dependency(service="frontend", host="127.0.0.1", port=15173)

    specification = FrontendAdapter(tmp_path, RecordingRunner()).start_specification(
        dependency
    )

    assert specification.command[-6:] == (
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        "15173",
        "--strictPort",
    )


def test_should_pass_dev_backend_endpoint_and_reload_to_launcher(tmp_path: Path) -> None:
    from adapters.backend import BackendAdapter

    dependency = _dependency(
        service="backend", host="127.0.0.1", port=8000, reload=True
    )

    specification = BackendAdapter(
        tmp_path, resolved_runtime_paths(tmp_path), RecordingRunner()
    ).start_specification(dependency)

    assert specification.command == (
        str(tmp_path / "scripts" / "start-backend.sh"),
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--reload",
    )


def test_should_not_pass_reload_to_dogfood_backend_launcher(tmp_path: Path) -> None:
    from adapters.backend import BackendAdapter

    dependency = _dependency(
        service="backend", host="localhost", port=18000, reload=False
    )

    specification = BackendAdapter(
        tmp_path, resolved_runtime_paths(tmp_path), RecordingRunner()
    ).start_specification(dependency)

    assert specification.command == (
        str(tmp_path / "scripts" / "start-backend.sh"),
        "--host",
        "localhost",
        "--port",
        "18000",
    )
    assert all(argument != "--reload" for argument in specification.command)


def test_should_not_reparse_backend_base_url_after_endpoint_resolution(
    tmp_path: Path,
) -> None:
    from adapters.backend import BackendAdapter

    dependency = _dependency(
        service="backend", host="localhost", port=18000, reload=False
    )
    dependency["baseUrl"] = "http://localhost:8000"

    specification = BackendAdapter(
        tmp_path, resolved_runtime_paths(tmp_path), RecordingRunner()
    ).start_specification(dependency)

    port_index = specification.command.index("--port")
    assert specification.command[port_index + 1] == "18000"


def test_should_use_resolved_endpoint_during_frontend_verification(tmp_path: Path) -> None:
    from adapters.frontend import FrontendAdapter

    dependency = _dependency(service="frontend", host="localhost", port=15173)

    result = FrontendAdapter(tmp_path, RecordingRunner()).verify(
        dependency,
        OperationContext(whisper_enabled=False, chroma_enabled=False),
    )

    assert result.checks


def test_should_not_reparse_frontend_base_url_after_endpoint_resolution(
    tmp_path: Path,
) -> None:
    from adapters.frontend import FrontendAdapter

    dependency = _dependency(service="frontend", host="localhost", port=15173)
    dependency["baseUrl"] = "http://localhost:5173"

    specification = FrontendAdapter(tmp_path, RecordingRunner()).start_specification(
        dependency
    )

    port_index = specification.command.index("--port")
    assert specification.command[port_index + 1] == "15173"
