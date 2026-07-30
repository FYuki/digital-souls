from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
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
    creator: str
    character_version: str
    extensions: Mapping[str, object]


@dataclass(frozen=True)
class CharacterCard:
    spec: str
    spec_version: str
    data: CharacterCardData


@dataclass(frozen=True)
class VoicevoxTtsConfig:
    speaker_id: int
