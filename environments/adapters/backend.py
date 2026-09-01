from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import RuntimePaths

from adapters.base import (
    AdapterOperationError,
    Check,
    CommandRunner,
    OperationContext,
    VerificationResult,
    require_resolved_managed_endpoint,
)
from adapters.compose_service import ComposeManagedServiceOperations


RUNTIME_UID_ENV = "DS_RUNTIME_UID"
RUNTIME_GID_ENV = "DS_RUNTIME_GID"


def _require_dogfood_runtime_identity() -> tuple[int, int]:
    values: list[int] = []
    for key in (RUNTIME_UID_ENV, RUNTIME_GID_ENV):
        raw_value = os.environ.get(key)
        if (
            raw_value is None
            or not raw_value.isascii()
            or not raw_value.isdecimal()
            or int(raw_value) < 1
            or str(int(raw_value)) != raw_value
        ):
            raise AdapterOperationError(
                "preparation",
                f"{key} must be a canonical positive integer in dogfood",
            )
        values.append(int(raw_value))
    return values[0], values[1]


def _chroma_storage_check(chroma_path: Path) -> Check:
    if chroma_path.is_dir():
        writable = os.access(chroma_path, os.W_OK | os.X_OK)
        return Check(
            "chroma-storage",
            "ready" if writable else "preparation_required",
            "Chroma storage path",
            writable,
        )
    if chroma_path.exists() or chroma_path.is_symlink():
        return Check(
            "chroma-storage",
            "preparation_required",
            "Chroma storage path",
            False,
        )

    existing_parent = chroma_path.parent
    while not existing_parent.exists() and not existing_parent.is_symlink():
        existing_parent = existing_parent.parent
    creatable = existing_parent.is_dir() and os.access(
        existing_parent, os.W_OK | os.X_OK
    )
    return Check(
        "chroma-storage",
        "preparation_required",
        "Chroma storage path",
        creatable,
    )


class BackendAdapter(ComposeManagedServiceOperations):
    def __init__(
        self,
        root_dir: Path,
        runtime_paths: RuntimePaths,
        runner: CommandRunner | None = None,
        *,
        effective_profile: str = "dev",
        whisper_model_name: str | None = None,
    ) -> None:
        # whisper_model_name は旧呼び出し側との短期互換用。model は共有serviceが所有する。
        del whisper_model_name
        super().__init__(
            root_dir,
            "backend",
            runtime_paths,
            runner,
            effective_profile=effective_profile,
        )
        self._runtime_paths = runtime_paths

    def verify(
        self,
        dependency: Mapping[str, object],
        context: OperationContext,
    ) -> VerificationResult:
        require_resolved_managed_endpoint(dependency, service="backend")
        checks = list(self.compose_verification().checks)
        if context.chroma_enabled:
            checks.append(_chroma_storage_check(self._runtime_paths.chroma_path))
        return VerificationResult(tuple(checks))

    def prepare(
        self,
        dependency: Mapping[str, object],
        context: OperationContext,
    ) -> None:
        require_resolved_managed_endpoint(dependency, service="backend")
        runtime_identity = (
            _require_dogfood_runtime_identity()
            if context.chroma_enabled and self.effective_profile == "dogfood"
            else None
        )
        initialize_runtime_data_root(self._runtime_paths, self.root_dir)
        if context.chroma_enabled:
            self._runtime_paths.chroma_path.mkdir(parents=True, exist_ok=True)
            if runtime_identity is not None:
                runtime_uid, runtime_gid = runtime_identity
                try:
                    os.chown(self._runtime_paths.chroma_path, runtime_uid, runtime_gid)
                    self._runtime_paths.chroma_path.chmod(0o750)
                except OSError as error:
                    raise AdapterOperationError(
                        "preparation",
                        "failed to converge Chroma storage ownership for dogfood: "
                        f"{error}",
                    ) from error
