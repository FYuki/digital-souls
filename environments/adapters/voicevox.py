from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Mapping, cast

from adapters.base import (
    AdapterOperationError,
    Check,
    CommandRunner,
    HttpServiceOperations,
    OperationContext,
    ReadinessValidationResult,
    ServiceStartResult,
    StopResult,
    SubprocessRunner,
    VerificationResult,
    command_succeeded,
    require_managed_endpoint,
)
from environment_constants import VOICEVOX_CONTAINER_NAME, VOICEVOX_SETUP_COMMAND


class VoicevoxPreparationError(AdapterOperationError):
    def __init__(self, message: str) -> None:
        super().__init__("preparation", message)


class VoicevoxInspectionError(RuntimeError):
    __slots__ = ()


def _container_is_missing(stderr: str) -> bool:
    normalized = stderr.lower()
    return "no such container" in normalized or "no such object" in normalized


def _container_identity(result: Mapping[str, object]) -> tuple[dict[str, str], bool]:
    try:
        raw = json.loads(str(result.get("stdout", "")))
        container = raw[0]
        identity = {
            "containerId": container["Id"],
            "startedAt": container["State"]["StartedAt"],
        }
        running = container["State"]["Running"]
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
        raise VoicevoxPreparationError("invalid VOICEVOX docker inspect response") from error
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise VoicevoxPreparationError("invalid VOICEVOX container identity")
    if not isinstance(running, bool):
        raise VoicevoxPreparationError("invalid VOICEVOX container running state")
    return cast(dict[str, str], identity), running


class VoicevoxAdapter(HttpServiceOperations):
    def __init__(self, root_dir: Path, runner: CommandRunner | None = None) -> None:
        self.root_dir = root_dir
        self.runner = runner if runner is not None else SubprocessRunner()

    def _inspect(self) -> tuple[dict[str, str], bool]:
        result = self.runner.run(
            ("docker", "inspect", VOICEVOX_CONTAINER_NAME), self.root_dir
        )
        if not command_succeeded(result):
            stderr = result.get("stderr")
            if not isinstance(stderr, str):
                raise VoicevoxInspectionError("invalid VOICEVOX docker inspect result")
            if _container_is_missing(stderr):
                raise VoicevoxPreparationError(
                    f"VOICEVOX container is missing; create it first with: {VOICEVOX_SETUP_COMMAND}"
                )
            raise VoicevoxInspectionError(
                f"failed to inspect VOICEVOX container: {stderr.strip()}"
            )
        return _container_identity(result)

    def verify(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> VerificationResult:
        require_managed_endpoint(dependency, service="voicevox", port=50021)
        if shutil.which("docker") is None:
            classification = "preparation_required"
            message = "Docker command is required"
        else:
            try:
                self._inspect()
                classification = "pending"
                message = "VOICEVOX container exists"
            except VoicevoxPreparationError as error:
                classification = "preparation_required"
                message = str(error)
            except VoicevoxInspectionError as error:
                classification = "preparation_required"
                message = str(error)
        return VerificationResult(
            (Check("voicevox-container", classification, message, False),)
        )

    def prepare(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> None:
        require_managed_endpoint(dependency, service="voicevox", port=50021)

    def start(
        self, dependency: Mapping[str, object], environment: Mapping[str, str]
    ) -> ServiceStartResult:
        require_managed_endpoint(dependency, service="voicevox", port=50021)
        identity, running = self._inspect()
        if running:
            return ServiceStartResult(
                state="reused", owned=False, container_identity=identity
            )
        result = self.runner.run(("docker", "start", VOICEVOX_CONTAINER_NAME), self.root_dir)
        if not command_succeeded(result):
            raise RuntimeError(f"VOICEVOX startup failed: {result.get('stderr', '')}")
        started_identity: dict[str, str] | None = None
        try:
            started_identity, started = self._inspect()
            if not started:
                raise RuntimeError("VOICEVOX container did not enter running state")
        except Exception as startup_error:
            self._rollback_failed_start(startup_error, started_identity)
        if started_identity is None:
            raise RuntimeError("VOICEVOX started without a container identity")
        return ServiceStartResult(
            state="started", owned=True, container_identity=started_identity
        )

    def _rollback_failed_start(
        self,
        startup_error: Exception,
        current_identity: dict[str, str] | None,
    ) -> None:
        if current_identity is None:
            try:
                current_identity, _running = self._inspect()
            except Exception as identity_error:
                raise AdapterOperationError(
                    "startup",
                    str(startup_error),
                    cleanup_failure=StopResult(
                        "failed", f"cannot identify started VOICEVOX container: {identity_error}"
                    ),
                ) from startup_error
        ownership = ServiceStartResult(
            state="started", owned=True, container_identity=current_identity
        )
        try:
            rollback = self.runner.run(
                ("docker", "stop", current_identity["containerId"]), self.root_dir
            )
        except Exception as rollback_error:
            raise AdapterOperationError(
                "startup",
                str(startup_error),
                ownership=ownership,
                cleanup_failure=StopResult("failed", str(rollback_error)),
            ) from startup_error
        if command_succeeded(rollback):
            raise startup_error
        cleanup_message = str(rollback.get("stderr", "docker stop failed"))
        raise AdapterOperationError(
            "startup",
            str(startup_error),
            ownership=ownership,
            cleanup_failure=StopResult("failed", cleanup_message),
        ) from startup_error

    def identity_matches(self, expected: Mapping[str, str]) -> bool:
        try:
            actual, running = self._inspect()
        except VoicevoxPreparationError:
            return False
        return running and actual == expected

    def validate_readiness(
        self, dependency: Mapping[str, object]
    ) -> ReadinessValidationResult:
        return ReadinessValidationResult("ready")

    def is_running(self, service: Mapping[str, object]) -> bool:
        return self.identity_matches(_container_identity_from_service(service))

    def stop(self, service: Mapping[str, object], grace_seconds: float) -> StopResult:
        expected = _container_identity_from_service(service)
        if not self.identity_matches(expected):
            return StopResult("skipped_identity_mismatch")
        result = self.runner.run(
            ("docker", "stop", expected["containerId"]), self.root_dir
        )
        if not command_succeeded(result):
            return StopResult("failed", str(result.get("stderr", "docker stop failed")))
        return StopResult("stopped")


def _container_identity_from_service(service: Mapping[str, object]) -> Mapping[str, str]:
    identity = service.get("containerIdentity")
    if not isinstance(identity, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in identity.items()
    ):
        raise ValueError("voicevox container identity is required")
    return cast(Mapping[str, str], identity)
