from pathlib import Path


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_LEGACY_NAMES = (
    "OLLAMA_" + "CHAT_MODEL",
    "OLLAMA_" + "CLASSIFIER_MODEL",
    "OLLAMA_" + "EXTRACTOR_MODEL",
    "OLLAMA_" + "EMBEDDING_MODEL",
    "OLLAMA_" + "CONTEXT_TOKENS",
    "OLLAMA_" + "RESPONSE_RESERVE_TOKENS",
)
_TEXT_SUFFIXES = {".json", ".md", ".py", ".sh", ".toml", ".yaml", ".yml"}


def test_legacy_inference_settings_do_not_return_to_runtime_or_current_docs() -> None:
    roots = (
        _REPOSITORY_ROOT / "backend" / "app",
        _REPOSITORY_ROOT / "environments",
        _REPOSITORY_ROOT / "docs",
    )
    files = [
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in _TEXT_SUFFIXES
        and "__pycache__" not in path.parts
        and "data" not in path.relative_to(root).parts
    ]
    files.extend(
        (
            _REPOSITORY_ROOT / ".env.example",
            _REPOSITORY_ROOT / "backend" / ".env.example",
        )
    )
    rejection_contract = (
        _REPOSITORY_ROOT / "backend" / "app" / "inference" / "config.py"
    )

    offenders: list[str] = []
    for path in files:
        if path == rejection_contract:
            continue
        content = path.read_text(encoding="utf-8")
        if any(name in content for name in _LEGACY_NAMES):
            offenders.append(str(path.relative_to(_REPOSITORY_ROOT)))

    assert offenders == []
