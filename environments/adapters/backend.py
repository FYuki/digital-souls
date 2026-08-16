from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from app.model_settings import (
    WHISPER_MODEL_NAME,
)
from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import RuntimePaths
from app.stt.whisper_client import WHISPER_COMPUTE_TYPE, WHISPER_DEVICE

from adapters.base import (
    AdapterError,
    Check,
    CommandRunner,
    OperationContext,
    ProcessServiceOperations,
    StartSpecification,
    VerificationResult,
    command_succeeded,
    require_resolved_managed_endpoint,
)

WHISPER_REQUIRED_ARTIFACTS = (
    "config.json",
    "model.bin",
    "preprocessor_config.json",
    "tokenizer.json",
    "vocabulary.json",
)
WHISPER_CACHE_LOOKUP = (
    "import sys; from faster_whisper.utils import download_model; "
    "print(download_model(sys.argv[1], cache_dir=sys.argv[2], local_files_only=True))"
)
WHISPER_MODEL_PREPARATION = (
    "import sys; from faster_whisper import WhisperModel; "
    "from app.stt.whisper_client import WHISPER_COMPUTE_TYPE, WHISPER_DEVICE; "
    "WhisperModel(sys.argv[1], download_root=sys.argv[2], "
    "device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)"
)
WHISPER_MINIMAL_INFERENCE = (
    "import sys; from pathlib import Path; "
    "from app.audio.constants import "
    "PCM_CHANNELS, PCM_SAMPLE_RATE_HZ, PCM_SAMPLE_WIDTH_BYTES; "
    "from app.stt.whisper_client import WhisperTranscriber; "
    "silence = bytes(PCM_SAMPLE_RATE_HZ * PCM_CHANNELS * PCM_SAMPLE_WIDTH_BYTES // 10); "
    "WhisperTranscriber(model_name=sys.argv[1], download_root=Path(sys.argv[2]))"
    ".transcribe(silence)"
)


def _whisper_model_is_ready(model_cache: Path) -> bool:
    return model_cache.is_dir() and all(
        (model_cache / artifact).is_file() for artifact in WHISPER_REQUIRED_ARTIFACTS
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
    def __init__(
        self,
        root_dir: Path,
        runtime_paths: RuntimePaths,
        runner: CommandRunner | None = None,
        *,
        whisper_model_name: str = WHISPER_MODEL_NAME,
    ) -> None:
        super().__init__(root_dir, "backend", runner)
        self._runtime_paths = runtime_paths
        self._whisper_model_name = whisper_model_name

    def _cached_whisper_model(self) -> Path | None:
        python = self.root_dir / "backend" / ".venv" / "bin" / "python"
        if not _is_executable_file(python):
            return None
        result = self.runner.run(
            (
                str(python),
                "-c",
                WHISPER_CACHE_LOOKUP,
                self._whisper_model_name,
                str(self._runtime_paths.whisper_cache_path),
            ),
            self.root_dir,
        )
        if not command_succeeded(result):
            return None
        stdout = result.get("stdout")
        if not isinstance(stdout, str) or not stdout.strip():
            return None
        return Path(stdout.strip())

    def verify(
        self,
        dependency: Mapping[str, object],
        context: OperationContext,
    ) -> VerificationResult:
        require_resolved_managed_endpoint(dependency, service="backend")
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
            model_cache = self._cached_whisper_model()
            checks.append(
                Check(
                    f"whisper-model-{self._whisper_model_name}",
                    (
                        "ready"
                        if model_cache is not None
                        and _whisper_model_is_ready(model_cache)
                        else "preparation_required"
                    ),
                    f"faster-whisper {self._whisper_model_name} model cache",
                    True,
                )
            )
        if context.chroma_enabled:
            checks.append(_chroma_storage_check(self._runtime_paths.chroma_path))
        return VerificationResult(tuple(checks))

    def prepare(
        self,
        dependency: Mapping[str, object],
        context: OperationContext,
    ) -> None:
        require_resolved_managed_endpoint(dependency, service="backend")
        initialize_runtime_data_root(self._runtime_paths, self.root_dir)
        result = self.runner.run((str(self.root_dir / "scripts" / "setup-backend.sh"),), self.root_dir)
        if not command_succeeded(result):
            raise RuntimeError(f"backend preparation failed: {result.get('stderr', '')}")
        if context.whisper_enabled:
            model_cache = self._runtime_paths.whisper_cache_path
            backend_dir = self.root_dir / "backend"
            cached_model = self._cached_whisper_model()
            if cached_model is None or not _whisper_model_is_ready(cached_model):
                command = (
                    str(self.root_dir / "backend" / ".venv" / "bin" / "python"),
                    "-c",
                    WHISPER_MODEL_PREPARATION,
                    self._whisper_model_name,
                    str(model_cache),
                )
                download = self.runner.run(command, backend_dir)
                if not command_succeeded(download):
                    raise RuntimeError(
                        f"Whisper model preparation failed: {download.get('stderr', '')}"
                    )
            inference = self.runner.run(
                (
                    str(self.root_dir / "backend" / ".venv" / "bin" / "python"),
                    "-c",
                    WHISPER_MINIMAL_INFERENCE,
                    self._whisper_model_name,
                    str(model_cache),
                ),
                backend_dir,
            )
            if not command_succeeded(inference):
                raise RuntimeError(
                    "Whisper minimal inference failed "
                    f"(device={WHISPER_DEVICE}, compute_type={WHISPER_COMPUTE_TYPE}): "
                    f"{inference.get('stderr', '')}. "
                    "Restore Backend dependencies with scripts/setup-backend.sh, "
                    "then retry with environments/up.sh."
                )
        if context.chroma_enabled:
            self._runtime_paths.chroma_path.mkdir(parents=True, exist_ok=True)

    def start_specification(self, dependency: Mapping[str, object]) -> StartSpecification:
        host, port = require_resolved_managed_endpoint(dependency, service="backend")
        reload_enabled = dependency.get("reload")
        if not isinstance(reload_enabled, bool):
            raise AdapterError("backend resolved reload is required")
        command = [
            str(self.root_dir / "scripts" / "start-backend.sh"),
            "--host",
            host,
            "--port",
            str(port),
        ]
        if reload_enabled:
            command.append("--reload")
        return StartSpecification(
            command=tuple(command),
            cwd=self.root_dir,
        )
