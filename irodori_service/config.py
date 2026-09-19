from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

MODEL_REPOSITORY = "Aratako/Irodori-TTS-v4.1-Small"
MODEL_REVISION = "2b28324dc263ed5e6638b3cf3dd94c82ead07b4b"
CODEC_REPOSITORY = "Aratako/Semantic-DACVAE-Japanese-32dim"
CODEC_REVISION = "47376ee24834d7a05a48ebabfe3cde29b3c5e214"
SERVER_REVISION = "841fb7c6ec57729c56b9b75c0ef2562249b13a10"
IRODORI_REVISION = "8ca3acb58ab4e19ad6d594aaed6bafe3e88f7f71"


@dataclass(frozen=True)
class ServiceConfig:
    voices_dir: Path
    model_cache: Path
    warmup_voice: str = "miori-b3-4221"
    cuda_graph: bool = False
    max_pending: int = 8
    max_voice_checks: int = 2
    queue_timeout: float = 10.0
    inference_timeout: float = 30.0
    startup_timeout: float = 300.0

    def __post_init__(self) -> None:
        if type(self.cuda_graph) is not bool:
            raise ValueError("cuda_graph must be boolean")
        if type(self.max_pending) is not int or self.max_pending < 1:
            raise ValueError("max_pending must be positive")
        if type(self.max_voice_checks) is not int or self.max_voice_checks < 1:
            raise ValueError("max_voice_checks must be positive")
        for value in (self.queue_timeout, self.inference_timeout, self.startup_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("timeouts must be finite and positive")


def load_config() -> ServiceConfig:
    graph = os.environ.get("DS_IRODORI_CUDA_GRAPH", "false")
    if graph not in {"true", "false"}:
        raise ValueError("DS_IRODORI_CUDA_GRAPH must be true or false")
    return ServiceConfig(
        voices_dir=Path(os.environ.get("DS_IRODORI_VOICES_DIR", "/voices")),
        model_cache=Path(os.environ.get("DS_IRODORI_MODEL_CACHE", "/models/huggingface")),
        warmup_voice=os.environ.get("DS_IRODORI_WARMUP_VOICE", "miori-b3-4221"),
        cuda_graph=graph == "true",
        max_pending=int(os.environ.get("DS_IRODORI_MAX_PENDING", "8")),
        max_voice_checks=int(os.environ.get("DS_IRODORI_MAX_VOICE_CHECKS", "2")),
        queue_timeout=float(os.environ.get("DS_IRODORI_QUEUE_TIMEOUT_SECONDS", "10")),
        inference_timeout=float(os.environ.get("DS_IRODORI_INFERENCE_TIMEOUT_SECONDS", "30")),
        startup_timeout=float(os.environ.get("DS_IRODORI_STARTUP_TIMEOUT_SECONDS", "300")),
    )
