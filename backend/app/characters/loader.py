import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, cast

from app.characters.models import (
    CharacterCard,
    CharacterCardData,
    VoicevoxTtsConfig,
)

CARD_FILE_SUFFIX = ".card.json"
CHARACTERS_DIR_NAME = "characters"
CARD_SPEC_FIELD = "spec"
CARD_SPEC_VERSION_FIELD = "spec_version"
CARD_V3_SPEC = "chara_card_v3"
CARD_V3_VERSION = "3.0"
DATA_FIELD = "data"
EXTENSIONS_FIELD = "extensions"
DIGITAL_SOULS_EXTENSION = "digital_souls"
TTS_CONFIG_FIELD = "tts_config"
TTS_ENGINE_FIELD = "engine"
TTS_SPEAKER_ID_FIELD = "speaker_id"
VOICEVOX_ENGINE = "voicevox"
CARD_DATA_INVALID_MESSAGE = "'data' field must be an object in character card"
TTS_CONFIG_MISSING_MESSAGE = (
    "'tts_config' field is missing in extensions.digital_souls"
)
TTS_CONFIG_INVALID_MESSAGE = (
    "'tts_config' field must be an object in extensions.digital_souls"
)


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


JsonObject = dict[str, object]


def _load_character_card(character: str) -> JsonObject:
    card_path = _build_character_file_path(character, f"{character}{CARD_FILE_SUFFIX}")
    if not card_path.is_file():
        raise FileNotFoundError(f"Character card not found: {card_path}")

    card = json.loads(card_path.read_text(encoding="utf-8"))
    if not isinstance(card, dict):
        raise ValueError("Character card must be a JSON object")
    return cast(JsonObject, card)


def _required_string(data: Mapping[str, object], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _string_tuple(data: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    value = data.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be an array of strings")
    return tuple(value)


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _extensions(data: Mapping[str, object]) -> Mapping[str, object]:
    value = data.get(EXTENSIONS_FIELD)
    if not isinstance(value, dict):
        raise ValueError(f"{EXTENSIONS_FIELD} must be an object")
    return cast(Mapping[str, object], _freeze_json(value))


def load_character_card(character: str) -> CharacterCard:
    card = _load_character_card(character)
    spec = _required_string(card, CARD_SPEC_FIELD)
    spec_version = _required_string(card, CARD_SPEC_VERSION_FIELD)
    if spec != CARD_V3_SPEC:
        raise ValueError(f"{CARD_SPEC_FIELD} must be '{CARD_V3_SPEC}'")
    if spec_version != CARD_V3_VERSION:
        raise ValueError(
            f"{CARD_SPEC_VERSION_FIELD} must be '{CARD_V3_VERSION}'"
        )

    data_value = card.get(DATA_FIELD)
    if not isinstance(data_value, dict):
        raise ValueError(CARD_DATA_INVALID_MESSAGE)
    data = cast(JsonObject, data_value)
    return CharacterCard(
        spec=spec,
        spec_version=spec_version,
        data=CharacterCardData(
            name=_required_string(data, "name"),
            description=_required_string(data, "description"),
            personality=_required_string(data, "personality"),
            scenario=_required_string(data, "scenario"),
            first_mes=_required_string(data, "first_mes"),
            mes_example=_required_string(data, "mes_example"),
            creator_notes=_required_string(data, "creator_notes"),
            system_prompt=_required_string(data, "system_prompt"),
            post_history_instructions=_required_string(
                data,
                "post_history_instructions",
            ),
            alternate_greetings=_string_tuple(data, "alternate_greetings"),
            group_only_greetings=_string_tuple(data, "group_only_greetings"),
            creator=_required_string(data, "creator"),
            character_version=_required_string(data, "character_version"),
            extensions=_extensions(data),
        ),
    )


def _digital_souls_extension(card: CharacterCard) -> Mapping[str, object]:
    value = card.data.extensions.get(DIGITAL_SOULS_EXTENSION)
    if not isinstance(value, Mapping):
        raise ValueError("extensions.digital_souls must be an object")
    return value


def load_tts_config(character: str) -> VoicevoxTtsConfig:
    digital_souls = _digital_souls_extension(load_character_card(character))
    if TTS_CONFIG_FIELD not in digital_souls:
        raise KeyError(TTS_CONFIG_MISSING_MESSAGE)
    tts_config = digital_souls[TTS_CONFIG_FIELD]
    if not isinstance(tts_config, Mapping):
        raise ValueError(TTS_CONFIG_INVALID_MESSAGE)

    if tts_config.get(TTS_ENGINE_FIELD) != VOICEVOX_ENGINE:
        raise ValueError("tts_config.engine must be 'voicevox'")
    speaker_id = tts_config.get(TTS_SPEAKER_ID_FIELD)
    if type(speaker_id) is not int:
        raise ValueError("tts_config.speaker_id must be an integer")
    return VoicevoxTtsConfig(speaker_id=speaker_id)
