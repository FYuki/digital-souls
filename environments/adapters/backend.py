from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from app.model_settings import (
    WHISPER_MODEL_CACHE_DIRECTORY,
    WHISPER_MODEL_NAME,
    whisper_model_cache,
)
from adapters.base import (
    Check,
    CommandRunner,
    OperationContext,
    ProcessServiceOperations,
    StartSpecification,
    VerificationResult,
    command_succeeded,
    require_managed_endpoint,
)


WHISPER_REQUIRED_ARTIFACTS = (
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.json",
)


def _whisper_model_is_ready(model_cache: Path) -> bool:
    snapshots = model_cache / "snapshots"
    return snapshots.is_dir() and any(
        snapshot.is_dir()
        and all((snapshot / artifact).is_file() for artifact in WHISPER_REQUIRED_ARTIFACTS)
        for snapshot in snapshots.iterdir()
    )


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


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


class BackendAdapter(ProcessServiceOperations):
    def __init__(self, root_dir: Path, runner: CommandRunner | None = None) -> None:
        super().__init__(root_dir, "backend", runner)

    def verify(
        self,
        dependency: Mapping[str, object],
        context: OperationContext,
    ) -> VerificationResult:
        require_managed_endpoint(dependency, service="backend", port=8000)
        venv = self.root_dir / "backend" / ".venv"
        setup_launcher = self.root_dir / "scripts" / "setup-backend.sh"
        start_launcher = self.root_dir / "scripts" / "start-backend.sh"
        checks = [
            Check(
                "backend-setup-launcher",
                (
                    "ready"
                    if _is_executable_file(setup_launcher)
                    else "preparation_required"
                ),
                "executable backend setup launcher",
                False,
            ),
            Check(
                "backend-start-launcher",
                (
                    "ready"
                    if _is_executable_file(start_launcher)
                    else "preparation_required"
                ),
                "executable backend start launcher",
                False,
            ),
            Check(
                "backend-python",
                (
                    "ready"
                    if _is_executable_file(venv / "bin" / "python")
                    else "preparation_required"
                ),
                "backend virtual environment Python",
                True,
            ),
            Check(
                "backend-uvicorn",
                (
                    "ready"
                    if _is_executable_file(venv / "bin" / "uvicorn")
                    else "preparation_required"
                ),
                "backend virtual environment",
                True,
            ),
        ]
        if context.whisper_enabled:
            model_cache = whisper_model_cache(self.root_dir) / WHISPER_MODEL_CACHE_DIRECTORY
            checks.append(
                Check(
                    "whisper-model-medium",
                    (
                        "ready"
                        if _whisper_model_is_ready(model_cache)
                        else "preparation_required"
                    ),
                    "faster-whisper medium model cache",
                    True,
                )
            )
        if context.chroma_enabled:
            chroma_path = self.root_dir / "backend" / "app" / "data" / "chroma"
            checks.append(_chroma_storage_check(chroma_path))
        return VerificationResult(tuple(checks))

    def prepare(
        self,
        dependency: Mapping[str, object],
        context: OperationContext,
    ) -> None:
        require_managed_endpoint(dependency, service="backend", port=8000)
        result = self.runner.run((str(self.root_dir / "scripts" / "setup-backend.sh"),), self.root_dir)
        if not command_succeeded(result):
            raise RuntimeError(f"backend preparation failed: {result.get('stderr', '')}")
        if context.whisper_enabled:
            model_cache = whisper_model_cache(self.root_dir)
            cached_model = model_cache / WHISPER_MODEL_CACHE_DIRECTORY
            if not _whisper_model_is_ready(cached_model):
                command = (
                    str(self.root_dir / "backend" / ".venv" / "bin" / "python"),
                    "-c",
                    "from faster_whisper import WhisperModel; "
                    f"WhisperModel({WHISPER_MODEL_NAME!r}, download_root={str(model_cache)!r})",
                )
                download = self.runner.run(command, self.root_dir)
                if not command_succeeded(download):
                    raise RuntimeError(
                        f"Whisper model preparation failed: {download.get('stderr', '')}"
                    )
        if context.chroma_enabled:
            (self.root_dir / "backend" / "app" / "data" / "chroma").mkdir(parents=True, exist_ok=True)

    def start_specification(self, dependency: Mapping[str, object]) -> StartSpecification:
        require_managed_endpoint(dependency, service="backend", port=8000)
        return StartSpecification(
            command=(str(self.root_dir / "scripts" / "start-backend.sh"),),
            cwd=self.root_dir,
        )
