import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

CARD_FILE_SUFFIX = ".card.json"
CHARACTERS_DIR_NAME = "characters"
PERSONALITY_FILE_NAME = "personality.md"
DATA_FIELD = "data"
TTS_CONFIG_FIELD = "tts_config"
TTS_ENGINE_FIELD = "engine"
TTS_SPEAKER_ID_FIELD = "speaker_id"
VOICEVOX_ENGINE = "voicevox"
CARD_DATA_MISSING_MESSAGE = "'data' field is missing in character card"
CARD_DATA_INVALID_MESSAGE = "'data' field must be an object in character card"
TTS_CONFIG_MISSING_MESSAGE = "'tts_config' field is missing in character card data"
TTS_CONFIG_INVALID_MESSAGE = "'tts_config' field must be an object in character card data"


@dataclass(frozen=True)
class VoicevoxTtsConfig:
    speaker_id: int


def _get_repo_root() -> Path:
    return Path(__file__).parent.parent.parent.parent


def _build_character_file_path(character: str, file_name: str) -> Path:
    repo_root = _get_repo_root()
    characters_root = (repo_root / CHARACTERS_DIR_NAME).resolve()
    character_file_path = (characters_root / character / file_name).resolve()

    try:
        character_file_path.relative_to(characters_root)
    except ValueError as exc:
        raise FileNotFoundError(f"Character file not found: {character_file_path}") from exc

    return character_file_path


def load_personality(character: str) -> str:
    personality_path = _build_character_file_path(character, PERSONALITY_FILE_NAME)
    if not personality_path.is_file():
        raise FileNotFoundError(f"Personality file not found: {personality_path}")
    return personality_path.read_text(encoding="utf-8")


JsonObject = dict[str, object]


def _load_character_card(character: str) -> JsonObject:
    card_path = _build_character_file_path(character, f"{character}{CARD_FILE_SUFFIX}")
    if not card_path.is_file():
        raise FileNotFoundError(f"Character card not found: {card_path}")

    card = json.loads(card_path.read_text(encoding="utf-8"))
    if not isinstance(card, dict):
        raise ValueError("Character card must be a JSON object")
    return cast(JsonObject, card)


def load_tts_config(character: str) -> VoicevoxTtsConfig:
    card = _load_character_card(character)
    if DATA_FIELD not in card:
        raise KeyError(CARD_DATA_MISSING_MESSAGE)
    data = card[DATA_FIELD]
    if not isinstance(data, dict):
        raise ValueError(CARD_DATA_INVALID_MESSAGE)

    if TTS_CONFIG_FIELD not in data:
        raise KeyError(TTS_CONFIG_MISSING_MESSAGE)
    tts_config = data[TTS_CONFIG_FIELD]
    if not isinstance(tts_config, dict):
        raise ValueError(TTS_CONFIG_INVALID_MESSAGE)

    if tts_config.get(TTS_ENGINE_FIELD) != VOICEVOX_ENGINE:
        raise ValueError("tts_config.engine must be 'voicevox'")
    speaker_id = tts_config.get(TTS_SPEAKER_ID_FIELD)
    if type(speaker_id) is not int:
        raise ValueError("tts_config.speaker_id must be an integer")
    return VoicevoxTtsConfig(speaker_id=speaker_id)
