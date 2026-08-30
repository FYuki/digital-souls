from __future__ import annotations

import json
import os
import re
import tempfile
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
    require_resolved_managed_endpoint,
)
from app.runtime_paths import RuntimePaths


IMMUTABLE_GHCR_IMAGE = re.compile(
    r"^ghcr\.io/[a-z0-9](?:[a-z0-9._-]*/)+[a-z0-9][a-z0-9._-]*@sha256:[0-9a-f]{64}$"
)
BACKEND_ENV_PREFIXES = (
    "DS_",
    "OLLAMA_",
    "VOICEVOX_",
    "LIVEKIT_",
    "RAG_",
    "MEMORY_",
    "CONVERSATION_",
    "USER_INPUT_",
    "ASSISTANT_",
    "LLM_",
    "WHISPER_",
)
BACKEND_ENV_KEYS = {
    "DOGFOOD_BACKUP_DIR",
    "DOGFOOD_BACKUP_RETENTION_COUNT",
    "DOGFOOD_BACKUP_AUTHENTICATION_KEY",
}
FRONTEND_ENV_KEYS = {
    "DS_BACKEND_ORIGIN",
    "DS_ENVIRONMENT_ID",
    "DS_PROFILE",
    "DS_PROFILE_REPORT",
}


class ComposeManagedServiceOperations(HttpServiceOperations):
    def __init__(
        self,
        root_dir: Path,
        label: str,
        runtime_paths: RuntimePaths,
        runner: CommandRunner | None,
        *,
        effective_profile: str,
    ) -> None:
        self.root_dir = root_dir
        self.label = label
        self.runtime_paths = runtime_paths
        self.runner = runner if runner is not None else SubprocessRunner()
        self.effective_profile = effective_profile

    def compose_verification(self) -> VerificationResult:
        result = self.runner.run(("docker", "compose", "version"), self.root_dir)
        ready = command_succeeded(result)
        return VerificationResult(
            (
                Check(
                    "docker-compose",
                    "ready" if ready else "preparation_required",
                    "Docker Engine and Compose plugin",
                    False,
                ),
            )
        )

    def start(
        self, dependency: Mapping[str, object], environment: Mapping[str, str]
    ) -> ServiceStartResult:
        host, port = require_resolved_managed_endpoint(dependency, service=self.label)
        if self._container_exists():
            raise AdapterOperationError(
                "startup",
                f"unowned {self.label} container already exists: {self._container_name()}",
            )
        compose_environment = self._write_compose_environment(
            dependency, environment, host=host, port=port
        )
        command = self._compose_command(compose_environment)
        if self.effective_profile == "dogfood":
            image = compose_environment[f"DS_{self.label.upper()}_IMAGE"]
            if IMMUTABLE_GHCR_IMAGE.fullmatch(image) is None:
                raise AdapterOperationError(
                    "preparation",
                    f"dogfood {self.label} image must be an immutable GHCR digest",
                )
            preparation = (*command, "pull", self.label)
        else:
            preparation = (*command, "build", self.label)
        prepared = self.runner.run(preparation, self.root_dir)
        if not command_succeeded(prepared):
            raise AdapterOperationError(
                "preparation",
                f"{self.label} container image preparation failed: {prepared.get('stderr', '')}",
            )
        started = self.runner.run(
            (*command, "up", "--detach", "--no-deps", "--no-build", self.label),
            self.root_dir,
        )
        if not command_succeeded(started):
            raise AdapterOperationError(
                "startup",
                f"{self.label} container startup failed: {started.get('stderr', '')}",
            )
        identity: dict[str, str] | None = None
        try:
            identity, running = self._inspect()
            if not running:
                raise RuntimeError(f"{self.label} container did not remain running")
        except Exception as error:
            self._rollback_failed_start(error, identity)
        if identity is None:
            raise RuntimeError(f"{self.label} container identity is unavailable")
        return ServiceStartResult(
            "started", True, container_identity=identity
        )

    def _write_compose_environment(
        self,
        dependency: Mapping[str, object],
        environment: Mapping[str, str],
        *,
        host: str,
        port: int,
    ) -> dict[str, str]:
        runtime_uid = environment.get("DS_RUNTIME_UID")
        runtime_gid = environment.get("DS_RUNTIME_GID")
        if self.effective_profile == "dogfood" and (
            runtime_uid is None or runtime_gid is None
        ):
            raise AdapterOperationError(
                "profile", "dogfood container UID/GID must be provided by systemd"
            )
        if self.label == "backend":
            values = {
                key: value
                for key, value in environment.items()
                if key in {"HOME", "TZ", "XDG_CACHE_HOME"} | BACKEND_ENV_KEYS
                or key.startswith(BACKEND_ENV_PREFIXES)
            }
        else:
            values = {
                key: value
                for key, value in environment.items()
                if key in FRONTEND_ENV_KEYS or key in {"HOME", "TZ"}
            }
        values.update(
            DS_REPOSITORY_ROOT=str(self.root_dir),
            DS_DATA_DIR=str(self.runtime_paths.data_root),
            DS_RUNTIME_UID=runtime_uid or str(os.getuid()),
            DS_RUNTIME_GID=runtime_gid or str(os.getgid()),
            DS_CONTAINER_MODE=(
                "production" if self.effective_profile == "dogfood" else "development"
            ),
            **{
                f"DS_{self.label.upper()}_HOST": host,
                f"DS_{self.label.upper()}_PORT": str(port),
            },
        )
        # Composeは対象serviceだけを操作する場合もfile全体を補間するため、
        # もう一方のmanaged serviceに必要な非秘密値も必ず定義する。
        values.setdefault("DS_BACKEND_HOST", "127.0.0.1")
        values.setdefault("DS_BACKEND_PORT", "8000")
        values.setdefault("DS_BACKEND_RELOAD", "false")
        values.setdefault("DS_FRONTEND_HOST", "127.0.0.1")
        values.setdefault("DS_FRONTEND_PORT", "5173")
        values.setdefault(
            "DS_BACKEND_ORIGIN",
            environment.get("DS_BACKEND_ORIGIN", "http://127.0.0.1:8000"),
        )
        if "DS_PROFILE_REPORT" in environment:
            values.setdefault("DS_PROFILE_REPORT", environment["DS_PROFILE_REPORT"])
        if self.label == "backend":
            reload_enabled = dependency.get("reload")
            if not isinstance(reload_enabled, bool):
                raise AdapterOperationError("profile", "backend reload must be resolved")
            values["DS_BACKEND_RELOAD"] = str(reload_enabled).lower()
        if self.effective_profile == "dogfood":
            dogfood_key = f"DOGFOOD_{self.label.upper()}_IMAGE"
            image = environment.get(dogfood_key)
            if image is None:
                raise AdapterOperationError("profile", f"{dogfood_key} is required")
            values[f"DS_{self.label.upper()}_IMAGE"] = image
        if self.effective_profile == "dogfood":
            config_directory = environment.get("DOGFOOD_CONFIG_DIR")
            if config_directory is None or not Path(config_directory).is_absolute():
                raise AdapterOperationError(
                    "profile", "DOGFOOD_CONFIG_DIR is required for dogfood containers"
                )
            directory = Path(config_directory) / "containers"
        else:
            directory = self.runtime_paths.runtime_report_dir / "containers"
        directory.mkdir(parents=True, exist_ok=True)
        if self.label == "frontend":
            profile_report = environment.get("DS_PROFILE_REPORT")
            if profile_report is None:
                raise AdapterOperationError(
                    "profile", "DS_PROFILE_REPORT is required for frontend"
                )
            source = Path(profile_report)
            try:
                if source.is_symlink() or not source.is_file():
                    raise OSError("resolved profile report is not a regular file")
                profile_copy = directory / "frontend-resolved-profile.json"
                _atomic_write(profile_copy, source.read_bytes(), mode=0o644)
            except OSError as error:
                raise AdapterOperationError(
                    "profile", f"frontend profile report cannot be staged: {error}"
                ) from error
            values["DS_PROFILE_REPORT"] = str(profile_copy)
        path = directory / f"{self.label}.env"
        values["DS_CONTAINER_ENV_FILE"] = str(path)
        _atomic_write(
            path,
            "".join(
                f"{key}={_compose_env_value(values[key])}\n" for key in sorted(values)
            ).encode("utf-8"),
            mode=0o600,
        )
        return values

    def _compose_command(self, environment: Mapping[str, str]) -> tuple[str, ...]:
        command = [
            "docker",
            "compose",
            "--env-file",
            environment["DS_CONTAINER_ENV_FILE"],
            "--file",
            str(self.root_dir / "infra" / "application" / "compose.yaml"),
        ]
        if self.effective_profile != "dogfood":
            command.extend(
                (
                    "--file",
                    str(self.root_dir / "infra" / "application" / "compose.dev.yaml"),
                )
            )
        return tuple(command)

    def _container_name(self) -> str:
        return f"digital-souls-{self.runtime_paths.environment_id}-{self.label}"

    def _inspect(self) -> tuple[dict[str, str], bool]:
        result = self.runner.run(
            ("docker", "inspect", self._container_name()), self.root_dir
        )
        if not command_succeeded(result):
            raise RuntimeError(f"failed to inspect {self.label} container")
        try:
            container = json.loads(cast(str, result["stdout"]))[0]
            identity = {
                "containerId": container["Id"],
                "startedAt": container["State"]["StartedAt"],
            }
            running = container["State"]["Running"]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
            raise RuntimeError(f"invalid {self.label} docker inspect response") from error
        if not all(isinstance(value, str) and value for value in identity.values()):
            raise RuntimeError(f"invalid {self.label} container identity")
        if not isinstance(running, bool):
            raise RuntimeError(f"invalid {self.label} container state")
        return identity, running

    def _container_exists(self) -> bool:
        result = self.runner.run(
            ("docker", "inspect", self._container_name()), self.root_dir
        )
        return command_succeeded(result)

    def _rollback_failed_start(
        self, error: Exception, identity: dict[str, str] | None
    ) -> None:
        if identity is None:
            try:
                identity, _running = self._inspect()
            except Exception as inspect_error:
                raise AdapterOperationError(
                    "startup",
                    str(error),
                    cleanup_failure=StopResult("failed", str(inspect_error)),
                ) from error
        ownership = ServiceStartResult(
            "started", True, container_identity=identity
        )
        stopped = self.runner.run(("docker", "rm", "--force", identity["containerId"]), self.root_dir)
        if command_succeeded(stopped):
            raise error
        raise AdapterOperationError(
            "startup",
            str(error),
            ownership=ownership,
            cleanup_failure=StopResult("failed", str(stopped.get("stderr", "docker rm failed"))),
        ) from error

    def validate_readiness(
        self, dependency: Mapping[str, object]
    ) -> ReadinessValidationResult:
        del dependency
        return ReadinessValidationResult("ready")

    def is_running(self, service: Mapping[str, object]) -> bool:
        expected = _container_identity(service, self.label)
        try:
            actual, running = self._inspect()
        except RuntimeError:
            return False
        return running and actual == expected

    def stop(self, service: Mapping[str, object], grace_seconds: float) -> StopResult:
        del grace_seconds
        expected = _container_identity(service, self.label)
        if not self.is_running(service):
            return StopResult("skipped_identity_mismatch")
        result = self.runner.run(
            ("docker", "rm", "--force", expected["containerId"]), self.root_dir
        )
        if not command_succeeded(result):
            return StopResult("failed", str(result.get("stderr", "docker rm failed")))
        return StopResult("stopped")


def _container_identity(
    service: Mapping[str, object], label: str
) -> Mapping[str, str]:
    identity = service.get("containerIdentity")
    if not isinstance(identity, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in identity.items()
    ):
        raise ValueError(f"{label} container identity is required")
    return cast(Mapping[str, str], identity)


def _compose_env_value(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise AdapterOperationError("profile", "container environment contains invalid bytes")
    return json.dumps(value, ensure_ascii=False)


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
