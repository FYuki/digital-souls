from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import urlsplit

from http_readiness import ReadinessResult, probe_http
from process_control import (
    ManagedProcess,
    ProcessIdentity,
    process_identity_matches,
    start_managed_process,
    stop_managed_process,
    stop_owned_process,
)


class AdapterError(RuntimeError):
    __slots__ = ()


class CommandRunner(Protocol):
    def run(self, command: tuple[str, ...], cwd: Path) -> dict[str, object]: ...


class SubprocessRunner:
    def run(self, command: tuple[str, ...], cwd: Path) -> dict[str, object]:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }


@dataclass(frozen=True)
class Check:
    name: str
    classification: str
    message: str
    can_prepare: bool


@dataclass(frozen=True)
class VerificationResult:
    checks: tuple[Check, ...]

    @property
    def prerequisites_ready(self) -> bool:
        return all(check.classification != "preparation_required" for check in self.checks)


@dataclass(frozen=True)
class ReadinessValidationResult:
    classification: str
    message: str | None = None

    def __post_init__(self) -> None:
        if self.classification not in {"ready", "preparation", "readiness"}:
            raise AdapterError(
                f"unknown readiness validation classification: {self.classification}"
            )
        if self.classification != "ready" and not self.message:
            raise AdapterError("failed readiness validation requires a message")


@dataclass(frozen=True)
class StartSpecification:
    command: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True)
class ServiceStartResult:
    state: str
    owned: bool
    process_identity: dict[str, int] | None = None
    container_identity: dict[str, str] | None = None


@dataclass(frozen=True)
class StopResult:
    result: str
    message: str | None = None


class AdapterOperationError(RuntimeError):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        ownership: ServiceStartResult | None = None,
        cleanup_failure: StopResult | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.ownership = ownership
        self.cleanup_failure = cleanup_failure


@dataclass(frozen=True)
class OperationContext:
    whisper_enabled: bool
    chroma_enabled: bool


class ServiceOperations(Protocol):
    def verify(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> VerificationResult: ...

    def prepare(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> None: ...

    def probe(
        self, dependency: Mapping[str, object], timeout_seconds: float
    ) -> ReadinessResult: ...

    def start(
        self, dependency: Mapping[str, object], environment: Mapping[str, str]
    ) -> ServiceStartResult: ...

    def validate_readiness(
        self, dependency: Mapping[str, object]
    ) -> ReadinessValidationResult: ...

    def is_running(self, service: Mapping[str, object]) -> bool: ...

    def stop(self, service: Mapping[str, object], grace_seconds: float) -> StopResult: ...


class HttpServiceOperations:
    def probe(
        self, dependency: Mapping[str, object], timeout_seconds: float
    ) -> ReadinessResult:
        readiness_url = dependency.get("readinessUrl")
        if not isinstance(readiness_url, str):
            raise AdapterError("service readinessUrl is required")
        return probe_http(readiness_url, timeout_seconds=timeout_seconds)


class ProcessServiceOperations(HttpServiceOperations, ABC):
    def __init__(
        self, root_dir: Path, label: str, runner: CommandRunner | None
    ) -> None:
        self.root_dir = root_dir
        self.label = label
        self.runner = runner if runner is not None else SubprocessRunner()
        self._process: ManagedProcess | None = None

    @abstractmethod
    def start_specification(
        self, dependency: Mapping[str, object]
    ) -> StartSpecification: ...

    def start(
        self, dependency: Mapping[str, object], environment: Mapping[str, str]
    ) -> ServiceStartResult:
        specification = self.start_specification(dependency)
        process = start_managed_process(
            label=self.label,
            command=specification.command,
            cwd=specification.cwd,
            env=environment,
        )
        self._process = process
        return ServiceStartResult("started", True, process.identity.to_report())

    def validate_readiness(
        self, dependency: Mapping[str, object]
    ) -> ReadinessValidationResult:
        return ReadinessValidationResult("ready")

    def is_running(self, service: Mapping[str, object]) -> bool:
        identity = ProcessIdentity.from_report(_process_identity(service, self.label))
        if self._process is not None and self._process.identity == identity:
            return self._process.process.poll() is None
        return process_identity_matches(identity)

    def stop(self, service: Mapping[str, object], grace_seconds: float) -> StopResult:
        identity = ProcessIdentity.from_report(_process_identity(service, self.label))
        if self._process is not None and self._process.identity == identity:
            result = stop_managed_process(self._process, grace_seconds=grace_seconds)
            self._process.process.wait(timeout=max(1.0, grace_seconds + 1.0))
        else:
            result = stop_owned_process(identity, grace_seconds=grace_seconds)
        return StopResult(result.result)


def _process_identity(
    service: Mapping[str, object], label: str
) -> Mapping[str, object]:
    identity = service.get("processIdentity")
    if not isinstance(identity, dict):
        raise ValueError(f"{label} process identity is required")
    return identity


def require_managed_endpoint(
    dependency: Mapping[str, object], *, service: str, port: int
) -> tuple[str, int]:
    base_url = dependency.get("baseUrl")
    if not isinstance(base_url, str):
        raise AdapterError(f"{service} managed baseUrl is required")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or parsed.port != port
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
    ):
        raise AdapterError(
            f"{service} managed baseUrl must be http://localhost:{port} or http://127.0.0.1:{port}"
        )
    return parsed.hostname, parsed.port


def command_succeeded(result: Mapping[str, object]) -> bool:
    return result.get("returncode") == 0
