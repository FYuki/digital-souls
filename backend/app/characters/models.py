from dataclasses import dataclass
from enum import Enum

JsonObject = dict[str, object]


class CharacterLorePosition(str, Enum):
    BEFORE_CHAR = "before_char"
    AFTER_CHAR = "after_char"


@dataclass(frozen=True, repr=False)
class CharacterBookEntry:
    keys: tuple[str, ...]
    content: str
    extensions: JsonObject
    enabled: bool
    insertion_order: int
    use_regex: bool
    case_sensitive: bool | None
    constant: bool | None
    name: str | None
    priority: int | None
    id: int | str | None
    comment: str | None
    selective: bool | None
    secondary_keys: tuple[str, ...] | None
    position: CharacterLorePosition | None
    extra_fields: JsonObject


@dataclass(frozen=True, repr=False)
class CharacterBook:
    name: str | None
    description: str | None
    scan_depth: int | None
    token_budget: int | None
    recursive_scanning: bool | None
    extensions: JsonObject
    entries: tuple[CharacterBookEntry, ...]
    extra_fields: JsonObject
