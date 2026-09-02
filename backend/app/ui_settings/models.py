from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class PortraitLayout(str, Enum):
    RIGHT = "right"
    BACKGROUND = "background"


@dataclass(frozen=True)
class UiPreferences:
    desktop_portrait_layout: PortraitLayout
    desktop_history_height_percent: int
    compact_history_height_percent: int


@dataclass(frozen=True)
class CharacterUiState:
    character_id: str
    visible: bool
    pin_order: int | None


@dataclass(frozen=True)
class ThreadPin:
    character_id: str
    conversation_id: UUID


@dataclass(frozen=True)
class UiSettingsSnapshot:
    user_id: str
    preferences: UiPreferences
    characters: tuple[CharacterUiState, ...]
    thread_pins: tuple[ThreadPin, ...]
