from __future__ import annotations

import json
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path
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
    require_fixed_managed_endpoint,
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
        raise OllamaPreparationError(
            f"Ollama model {model_name} is required. Use the service account, HOME, "
            "and OLLAMA_MODELS configured for the external Ollama service. Run: "
            f"ollama pull {shlex.quote(model_name)}"
        )


class OllamaAdapter(ProcessServiceOperations):
    def __init__(
        self,
        root_dir: Path,
        runner: CommandRunner | None = None,
        *,
        model_name: str = OLLAMA_MODEL_NAME,
        classifier_model_name: str | None = None,
    ) -> None:
        super().__init__(root_dir, "ollama", runner)
        self._model_names = tuple(
            dict.fromkeys(
                (model_name, classifier_model_name or model_name)
            )
        )

    def verify(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> VerificationResult:
        require_fixed_managed_endpoint(dependency, service="ollama", port=11434)
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
        require_fixed_managed_endpoint(dependency, service="ollama", port=11434)
        for model_name in self._model_names:
            result = self.runner.run(("ollama", "pull", model_name), self.root_dir)
            if result.get("returncode") != 0:
                raise OllamaPreparationError(
                    "Ollama model preparation failed "
                    f"for {model_name}: {result.get('stderr', '')}"
                )

    def start_specification(self, dependency: Mapping[str, object]) -> StartSpecification:
        require_fixed_managed_endpoint(dependency, service="ollama", port=11434)
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
        for model_name in self._model_names:
            try:
                verify_required_model(payload, model_name)
            except OllamaPreparationError as error:
                return ReadinessValidationResult("preparation", str(error))
        return ReadinessValidationResult("ready")
