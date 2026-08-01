from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Mapping
from urllib.request import urlopen

from app.model_settings import OLLAMA_MODEL_NAME
from adapters.base import (
    AdapterOperationError,
    Check,
    CommandRunner,
    OperationContext,
    ProcessServiceOperations,
    ReadinessValidationResult,
    StartSpecification,
    VerificationResult,
    require_managed_endpoint,
)


class OllamaPreparationError(AdapterOperationError):
    def __init__(self, message: str) -> None:
        super().__init__("preparation", message)


def _fetch_json(url: str) -> dict[str, object]:
    with urlopen(url, timeout=1.0) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("Ollama tags response must be a JSON object")
    return value


def verify_required_model(
    payload: Mapping[str, object], model_name: str = OLLAMA_MODEL_NAME
) -> None:
    models = payload.get("models")
    names = {
        model.get("name")
        for model in models
        if isinstance(models, list) and isinstance(model, dict)
    } if isinstance(models, list) else set()
    if model_name not in names:
        raise OllamaPreparationError(f"Ollama model {model_name} is required")


class OllamaAdapter(ProcessServiceOperations):
    def __init__(
        self,
        root_dir: Path,
        runner: CommandRunner | None = None,
        *,
        model_name: str = OLLAMA_MODEL_NAME,
    ) -> None:
        super().__init__(root_dir, "ollama", runner)
        self._model_name = model_name

    def verify(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> VerificationResult:
        require_managed_endpoint(dependency, service="ollama", port=11434)
        return VerificationResult(
            (
                Check(
                    "ollama-command",
                    "pending" if shutil.which("ollama") else "preparation_required",
                    "Ollama command and model",
                    False,
                ),
            )
        )

    def prepare(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> None:
        require_managed_endpoint(dependency, service="ollama", port=11434)
        result = self.runner.run(("ollama", "pull", self._model_name), self.root_dir)
        if result.get("returncode") != 0:
            raise OllamaPreparationError(
                f"Ollama model preparation failed: {result.get('stderr', '')}"
            )

    def start_specification(self, dependency: Mapping[str, object]) -> StartSpecification:
        require_managed_endpoint(dependency, service="ollama", port=11434)
        return StartSpecification(command=("ollama", "serve"), cwd=self.root_dir)

    def validate_readiness(
        self, dependency: Mapping[str, object]
    ) -> ReadinessValidationResult:
        try:
            payload = _fetch_json(str(dependency["readinessUrl"]))
        except (OSError, ValueError) as error:
            return ReadinessValidationResult(
                "readiness", f"Ollama tags request failed: {error}"
            )
        try:
            verify_required_model(payload, self._model_name)
        except OllamaPreparationError as error:
            return ReadinessValidationResult("preparation", str(error))
        return ReadinessValidationResult("ready")
