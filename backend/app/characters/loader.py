import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.prompting import CharacterPrompt

CARD_FILE_SUFFIX = ".card.json"
CHARACTERS_DIR_NAME = "characters"
CARD_SPEC = "chara_card_v3"
CARD_SPEC_VERSION = "3.0"
DATA_FIELD = "data"
EXTENSIONS_FIELD = "extensions"
DIGITAL_SOULS_EXTENSION = "digital_souls"
TTS_CONFIG_FIELD = "tts_config"
TTS_ENGINE_FIELD = "engine"
TTS_SPEAKER_ID_FIELD = "speaker_id"
VOICEVOX_ENGINE = "voicevox"

ROOT_FIELDS = frozenset(("spec", "spec_version", DATA_FIELD))
REQUIRED_DATA_STRING_FIELDS = (
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "system_prompt",
)
OPTIONAL_DATA_STRING_FIELDS = (
    "creator",
    "character_version",
    "creator_notes",
    "post_history_instructions",
)
DATA_LIST_FIELDS = (
    "alternate_greetings",
    "group_only_greetings",
    "tags",
)
DATA_FIELDS = frozenset(
    REQUIRED_DATA_STRING_FIELDS
    + OPTIONAL_DATA_STRING_FIELDS
    + DATA_LIST_FIELDS
    + (EXTENSIONS_FIELD,)
)

JsonObject = dict[str, object]


class CharacterCardValidationError(ValueError):
    pass


class UnsupportedCharacterCardError(CharacterCardValidationError):
    def __init__(self, field: str, actual: object) -> None:
        self.field = field
        self.actual = actual
        super().__init__(f"unsupported Character Card {field}")


class TtsConfigMissingError(KeyError):
    pass


class TtsConfigValidationError(ValueError):
    pass


@dataclass(frozen=True)
class VoicevoxTtsConfig:
    speaker_id: int


@dataclass(frozen=True, repr=False)
class CharacterCardData:
    name: str
    description: str
    personality: str
    scenario: str
    first_mes: str
    mes_example: str
    creator_notes: str
    system_prompt: str
    post_history_instructions: str
    alternate_greetings: tuple[str, ...]
    group_only_greetings: tuple[str, ...]
    tags: tuple[str, ...]
    creator: str
    character_version: str
    extensions: JsonObject
    extra_fields: JsonObject

    def to_character_prompt(self) -> CharacterPrompt:
        return CharacterPrompt(
            description=self.description,
            personality=self.personality,
            scenario=self.scenario,
            system_prompt=self.system_prompt,
            mes_example=self.mes_example,
            post_history_instructions=self.post_history_instructions,
        )


@dataclass(frozen=True, repr=False)
class CharacterCard:
    spec: str
    spec_version: str
    data: CharacterCardData
    extra_fields: JsonObject

    def to_character_prompt(self) -> CharacterPrompt:
        return self.data.to_character_prompt()


def _get_repo_root() -> Path:
    return Path(__file__).parent.parent.parent.parent


def _build_character_file_path(character: str, file_name: str) -> Path:
    characters_root = (_get_repo_root() / CHARACTERS_DIR_NAME).resolve()
    character_file_path = (characters_root / character / file_name).resolve()
    try:
        character_file_path.relative_to(characters_root)
    except ValueError as exc:
        raise FileNotFoundError(
            f"Character file not found: {character_file_path}"
        ) from exc
    return character_file_path


def _load_character_card_document(character: str) -> JsonObject:
    card_path = _build_character_file_path(
        character,
        f"{character}{CARD_FILE_SUFFIX}",
    )
    if not card_path.is_file():
        raise FileNotFoundError(f"Character card not found: {card_path}")
    raw_document = json.loads(card_path.read_text(encoding="utf-8"))
    if not isinstance(raw_document, dict):
        raise CharacterCardValidationError(
            "Character Card root must be an object"
        )
    return cast(JsonObject, raw_document)


def load_character_card(character: str) -> CharacterCard:
    document = _load_character_card_document(character)
    _validate_card_identity(document)
    data = _required_object(document, DATA_FIELD, "Character Card root")
    return CharacterCard(
        spec=CARD_SPEC,
        spec_version=CARD_SPEC_VERSION,
        data=_parse_card_data(data),
        extra_fields=_extra_fields(document, ROOT_FIELDS),
    )


def load_tts_config(character: str) -> VoicevoxTtsConfig:
    card = load_character_card(character)
    extensions = card.data.extensions
    if DIGITAL_SOULS_EXTENSION not in extensions:
        raise TtsConfigMissingError(
            "digital_souls extension is missing in character card"
        )
    digital_souls = extensions[DIGITAL_SOULS_EXTENSION]
    if not isinstance(digital_souls, dict):
        raise TtsConfigValidationError(
            "digital_souls extension must be an object"
        )
    if TTS_CONFIG_FIELD not in digital_souls:
        raise TtsConfigMissingError(
            "tts_config is missing in digital_souls extension"
        )
    tts_config = digital_souls[TTS_CONFIG_FIELD]
    if not isinstance(tts_config, dict):
        raise TtsConfigValidationError("tts_config must be an object")
    if tts_config.get(TTS_ENGINE_FIELD) != VOICEVOX_ENGINE:
        raise TtsConfigValidationError("tts_config.engine must be 'voicevox'")
    speaker_id = tts_config.get(TTS_SPEAKER_ID_FIELD)
    if type(speaker_id) is not int:
        raise TtsConfigValidationError(
            "tts_config.speaker_id must be an integer"
        )
    return VoicevoxTtsConfig(speaker_id=speaker_id)


def _validate_card_identity(document: JsonObject) -> None:
    for field, expected in (
        ("spec", CARD_SPEC),
        ("spec_version", CARD_SPEC_VERSION),
    ):
        if field not in document:
            raise CharacterCardValidationError(
                f"Character Card root requires '{field}'"
            )
        actual = document[field]
        if actual != expected or type(actual) is not str:
            raise UnsupportedCharacterCardError(field, actual)


def _parse_card_data(data: JsonObject) -> CharacterCardData:
    return CharacterCardData(
        name=_required_string(data, "name"),
        description=_required_string(data, "description"),
        personality=_required_string(data, "personality"),
        scenario=_required_string(data, "scenario"),
        first_mes=_required_string(data, "first_mes"),
        mes_example=_required_string(data, "mes_example"),
        creator_notes=_optional_string(data, "creator_notes"),
        system_prompt=_required_string(data, "system_prompt"),
        post_history_instructions=_optional_string(
            data,
            "post_history_instructions",
        ),
        alternate_greetings=_string_tuple(data, "alternate_greetings"),
        group_only_greetings=_string_tuple(data, "group_only_greetings"),
        tags=_string_tuple(data, "tags"),
        creator=_optional_string(data, "creator"),
        character_version=_optional_string(data, "character_version"),
        extensions=_optional_object(data, EXTENSIONS_FIELD),
        extra_fields=_extra_fields(data, DATA_FIELDS),
    )


def _required_string(source: JsonObject, field: str) -> str:
    if field not in source or not isinstance(source[field], str):
        raise CharacterCardValidationError(
            f"Character Card data requires string field '{field}'"
        )
    return cast(str, source[field])


def _optional_string(source: JsonObject, field: str) -> str:
    if field not in source:
        return ""
    value = source[field]
    if not isinstance(value, str):
        raise CharacterCardValidationError(
            f"Character Card data field '{field}' must be a string"
        )
    return value


def _string_tuple(source: JsonObject, field: str) -> tuple[str, ...]:
    if field not in source:
        return ()
    value = source[field]
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise CharacterCardValidationError(
            f"Character Card data field '{field}' must be a string array"
        )
    return tuple(value)


def _optional_object(source: JsonObject, field: str) -> JsonObject:
    if field not in source:
        return {}
    value = source[field]
    if not isinstance(value, dict):
        raise CharacterCardValidationError(
            f"Character Card data field '{field}' must be an object"
        )
    return cast(JsonObject, value)


def _required_object(
    source: JsonObject,
    field: str,
    owner: str,
) -> JsonObject:
    if field not in source:
        raise CharacterCardValidationError(f"{owner} requires '{field}'")
    value = source[field]
    if not isinstance(value, dict):
        raise CharacterCardValidationError(
            f"{owner} field '{field}' must be an object"
        )
    return cast(JsonObject, value)


def _extra_fields(source: JsonObject, known_fields: frozenset[str]) -> JsonObject:
    return {
        field: value
        for field, value in source.items()
        if field not in known_fields
    }
