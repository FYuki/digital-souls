from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.characters.loader import (
    CARD_FILE_SUFFIX,
    CharacterCardValidationError,
    _load_character_card_path,
    _parse_character_card,
)

CHARACTER_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
STANDING_VARIANT_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULT_STANDING_VARIANT = "default"


class CharacterCatalogError(ValueError):
    """キャラクターカタログまたはアセットの解決に失敗した。"""


class CharacterNotFoundError(CharacterCatalogError):
    pass


class StandingImageNotConfiguredError(CharacterCatalogError):
    pass


class StandingImageNotFoundError(CharacterCatalogError):
    pass


class StandingImageLoadError(CharacterCatalogError):
    pass


@dataclass(frozen=True)
class StandingImageMetadata:
    status: Literal["available", "missing"]
    url: str | None


@dataclass(frozen=True)
class CharacterCatalogEntry:
    character_id: str
    display_name: str
    standing_image: StandingImageMetadata


class CharacterCatalog:
    def __init__(self, characters_root: Path) -> None:
        self._characters_root = characters_root.resolve()

    def scan(self) -> tuple[CharacterCatalogEntry, ...]:
        """ディスクを毎回再走査し、有効なCharacter Cardだけを返す。"""
        if not self._characters_root.is_dir():
            return ()
        entries: list[CharacterCatalogEntry] = []
        for character_dir in sorted(
            self._characters_root.iterdir(), key=lambda path: path.name
        ):
            entry = self._scan_character(character_dir)
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    def load_standing_image(self, character_id: str, variant: str) -> bytes:
        if not is_valid_character_id(character_id):
            raise CharacterNotFoundError(character_id)
        if not is_valid_standing_variant(variant):
            raise StandingImageNotFoundError(variant)
        character_dir = self._valid_character_directory(character_id)
        if character_dir is None or self._scan_character(character_dir) is None:
            raise CharacterNotFoundError(character_id)
        image_path = character_dir / "assets" / "standing" / f"{variant}.png"
        if image_path.is_symlink() or not image_path.is_file():
            if variant == DEFAULT_STANDING_VARIANT:
                raise StandingImageNotConfiguredError(character_id)
            raise StandingImageNotFoundError(variant)
        try:
            resolved_image = image_path.resolve(strict=True)
            resolved_image.relative_to(character_dir)
            resolved_image.relative_to(self._characters_root)
            content = resolved_image.read_bytes()
        except (OSError, ValueError) as error:
            raise StandingImageLoadError(character_id) from error
        if not content.startswith(PNG_SIGNATURE):
            raise StandingImageLoadError(character_id)
        return content

    def _scan_character(self, character_dir: Path) -> CharacterCatalogEntry | None:
        character_id = character_dir.name
        if (
            not is_valid_character_id(character_id)
            or character_dir.is_symlink()
            or not character_dir.is_dir()
        ):
            return None
        try:
            resolved_character = character_dir.resolve(strict=True)
            if resolved_character.parent != self._characters_root:
                return None
        except OSError:
            return None
        card_path = resolved_character / f"{character_id}{CARD_FILE_SUFFIX}"
        if card_path.is_symlink() or not card_path.is_file():
            return None
        try:
            card = _parse_character_card(_load_character_card_path(card_path))
        except (
            CharacterCardValidationError,
            FileNotFoundError,
            json.JSONDecodeError,
            OSError,
            UnicodeDecodeError,
        ):
            return None
        image_path = (
            resolved_character
            / "assets"
            / "standing"
            / f"{DEFAULT_STANDING_VARIANT}.png"
        )
        image_available = image_path.is_file() and not image_path.is_symlink()
        return CharacterCatalogEntry(
            character_id=character_id,
            display_name=card.data.name,
            standing_image=StandingImageMetadata(
                status="available" if image_available else "missing",
                url=(
                    standing_image_url(character_id, DEFAULT_STANDING_VARIANT)
                    if image_available
                    else None
                ),
            ),
        )

    def _valid_character_directory(self, character_id: str) -> Path | None:
        character_dir = self._characters_root / character_id
        if character_dir.is_symlink() or not character_dir.is_dir():
            return None
        try:
            resolved = character_dir.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != self._characters_root:
            return None
        return resolved


def is_valid_character_id(character_id: str) -> bool:
    return CHARACTER_ID_PATTERN.fullmatch(character_id) is not None


def is_valid_standing_variant(variant: str) -> bool:
    return STANDING_VARIANT_PATTERN.fullmatch(variant) is not None


def standing_image_url(character_id: str, variant: str) -> str:
    return f"/api/characters/{character_id}/assets/standing/{variant}.png"
