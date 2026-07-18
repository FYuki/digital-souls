from pathlib import Path


OLLAMA_MODEL_NAME = "gemma4:e4b"
WHISPER_MODEL_NAME = "medium"
WHISPER_MODEL_CACHE_DIRECTORY = "models--Systran--faster-whisper-medium"


def whisper_model_cache(repository_root: Path) -> Path:
    return repository_root / ".cache" / "huggingface" / "hub"
