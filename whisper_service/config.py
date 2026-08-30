from __future__ import annotations

import os
from dataclasses import dataclass


WHISPER_MODEL_ENV = "WHISPER_MODEL"
WHISPER_MODEL_REVISION_ENV = "WHISPER_MODEL_REVISION"
WHISPER_MODEL_PATH_ENV = "WHISPER_MODEL_PATH"
WHISPER_MODEL_CACHE_ENV = "WHISPER_MODEL_CACHE"
WHISPER_BAKED_MODEL_REVISION_ENV = "WHISPER_BAKED_MODEL_REVISION"
WHISPER_INFERENCE_TIMEOUT_SECONDS_ENV = "WHISPER_INFERENCE_TIMEOUT_SECONDS"

WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE_TYPE = "int8_float16"
WHISPER_DEVICE_INDEX = 0
DEFAULT_WHISPER_MODEL = "medium"
DEFAULT_WHISPER_MODEL_REVISION = "08e178d48790749d25932bbc082711ddcfdfbc4f"
DEFAULT_MODEL_CACHE = "/models/whisper"
DEFAULT_INFERENCE_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True)
class WhisperServiceConfig:
    model: str
    model_revision: str
    model_path: str
    model_cache: str
    inference_timeout_seconds: float
    device: str = WHISPER_DEVICE
    compute_type: str = WHISPER_COMPUTE_TYPE
    device_index: int = WHISPER_DEVICE_INDEX


def load_config(environment: dict[str, str] | None = None) -> WhisperServiceConfig:
    env = dict(os.environ) if environment is None else environment
    model = _canonical(env.get(WHISPER_MODEL_ENV, DEFAULT_WHISPER_MODEL), WHISPER_MODEL_ENV)
    revision = _canonical(
        env.get(WHISPER_MODEL_REVISION_ENV, DEFAULT_WHISPER_MODEL_REVISION),
        WHISPER_MODEL_REVISION_ENV,
    )
    baked_revision = env.get(WHISPER_BAKED_MODEL_REVISION_ENV)
    if baked_revision is not None and revision != baked_revision:
        raise ValueError(
            "WHISPER_MODEL_REVISION must match the model revision baked into the image"
        )
    cache = _canonical(env.get(WHISPER_MODEL_CACHE_ENV, DEFAULT_MODEL_CACHE), WHISPER_MODEL_CACHE_ENV)
    model_path = _canonical(
        env.get(WHISPER_MODEL_PATH_ENV, model), WHISPER_MODEL_PATH_ENV
    )
    timeout = _positive_float(
        env.get(WHISPER_INFERENCE_TIMEOUT_SECONDS_ENV),
        DEFAULT_INFERENCE_TIMEOUT_SECONDS,
        WHISPER_INFERENCE_TIMEOUT_SECONDS_ENV,
    )
    return WhisperServiceConfig(
        model=model,
        model_revision=revision,
        model_path=model_path,
        model_cache=cache,
        inference_timeout_seconds=timeout,
    )


def _canonical(value: str, field: str) -> str:
    if not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty canonical string")
    return value


def _positive_float(value: str | None, default: float, field: str) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{field} must be a positive number") from error
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive number")
    return parsed
