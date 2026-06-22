from pathlib import Path


def _get_repo_root() -> Path:
    return Path(__file__).parent.parent.parent.parent


def _build_personality_path(character: str) -> Path:
    repo_root = _get_repo_root()
    characters_root = (repo_root / "characters").resolve()
    personality_path = (characters_root / character / "personality.md").resolve()

    try:
        personality_path.relative_to(characters_root)
    except ValueError as exc:
        raise FileNotFoundError(
            f"Personality file not found: {personality_path}"
        ) from exc

    return personality_path


def load_personality(character: str) -> str:
    personality_path = _build_personality_path(character)
    if not personality_path.is_file():
        raise FileNotFoundError(f"Personality file not found: {personality_path}")
    return personality_path.read_text(encoding="utf-8")
