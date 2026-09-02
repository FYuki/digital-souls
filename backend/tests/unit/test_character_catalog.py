import json
from pathlib import Path

import pytest

from app.characters.catalog import (
    CharacterCatalog,
    CharacterNotFoundError,
    StandingImageLoadError,
    StandingImageNotConfiguredError,
    StandingImageNotFoundError,
    is_valid_character_id,
    is_valid_standing_variant,
)

PNG = b"\x89PNG\r\n\x1a\nimage"


def _card(name: str) -> dict[str, object]:
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": name,
            "description": "description",
            "personality": "personality",
            "scenario": "scenario",
            "first_mes": "hello",
            "mes_example": "example",
            "system_prompt": "prompt",
        },
    }


def _write_character(root: Path, character_id: str, name: str) -> Path:
    directory = root / character_id
    directory.mkdir(parents=True)
    (directory / f"{character_id}.card.json").write_text(
        json.dumps(_card(name)),
        encoding="utf-8",
    )
    return directory


def test_scan_returns_only_valid_character_cards(tmp_path: Path) -> None:
    root = tmp_path / "characters"
    _write_character(root, "miori", "光織")
    _write_character(root, "invalid_name", "無効なID")
    broken = root / "broken"
    broken.mkdir()
    (broken / "broken.card.json").write_text("{}", encoding="utf-8")
    missing = root / "missing"
    missing.mkdir()

    entries = CharacterCatalog(root).scan()

    assert [(entry.character_id, entry.display_name) for entry in entries] == [
        ("miori", "光織")
    ]
    assert entries[0].standing_image.status == "missing"
    assert entries[0].standing_image.url is None


def test_scan_detects_additions_and_card_changes_without_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "characters"
    miori = _write_character(root, "miori", "光織")
    catalog = CharacterCatalog(root)

    assert [entry.display_name for entry in catalog.scan()] == ["光織"]

    (miori / "miori.card.json").write_text(
        json.dumps(_card("新しい光織")),
        encoding="utf-8",
    )
    _write_character(root, "second-character", "二人目")

    assert [entry.display_name for entry in catalog.scan()] == [
        "新しい光織",
        "二人目",
    ]


def test_scan_exposes_safe_default_standing_image_url(tmp_path: Path) -> None:
    root = tmp_path / "characters"
    directory = _write_character(root, "miori", "光織")
    standing = directory / "assets" / "standing"
    standing.mkdir(parents=True)
    (standing / "default.png").write_bytes(PNG)

    entry = CharacterCatalog(root).scan()[0]

    assert entry.standing_image.status == "available"
    assert entry.standing_image.url == (
        "/api/characters/miori/assets/standing/default.png"
    )


def test_load_standing_image_rejects_cross_character_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "characters"
    miori = _write_character(root, "miori", "光織")
    other = _write_character(root, "other", "他")
    other_standing = other / "assets" / "standing"
    other_standing.mkdir(parents=True)
    other_image = other_standing / "default.png"
    other_image.write_bytes(PNG)
    miori_standing = miori / "assets" / "standing"
    miori_standing.mkdir(parents=True)
    (miori_standing / "default.png").symlink_to(other_image)

    with pytest.raises(StandingImageNotConfiguredError):
        CharacterCatalog(root).load_standing_image("miori", "default")


def test_load_standing_image_rejects_symlinked_parent_outside_characters(
    tmp_path: Path,
) -> None:
    root = tmp_path / "characters"
    miori = _write_character(root, "miori", "光織")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "default.png").write_bytes(PNG)
    assets = miori / "assets"
    assets.mkdir()
    (assets / "standing").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StandingImageLoadError):
        CharacterCatalog(root).load_standing_image("miori", "default")


def test_load_standing_image_distinguishes_missing_variant_and_invalid_png(
    tmp_path: Path,
) -> None:
    root = tmp_path / "characters"
    miori = _write_character(root, "miori", "光織")
    standing = miori / "assets" / "standing"
    standing.mkdir(parents=True)
    (standing / "default.png").write_bytes(b"not-png")
    catalog = CharacterCatalog(root)

    with pytest.raises(StandingImageLoadError):
        catalog.load_standing_image("miori", "default")
    with pytest.raises(StandingImageNotFoundError):
        catalog.load_standing_image("miori", "other")


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("miori", True),
        ("second-character2", True),
        ("Miori", False),
        ("miori_2", False),
        ("../miori", False),
        ("-miori", False),
        ("miori--night", False),
        ("光織", False),
    ],
)
def test_character_and_variant_names_use_ascii_lowercase_kebab_case(
    value: str,
    valid: bool,
) -> None:
    assert is_valid_character_id(value) is valid
    assert is_valid_standing_variant(value) is valid


def test_load_standing_image_rejects_unknown_and_traversal_ids(
    tmp_path: Path,
) -> None:
    catalog = CharacterCatalog(tmp_path / "characters")

    with pytest.raises(CharacterNotFoundError):
        catalog.load_standing_image("../miori", "default")
    with pytest.raises(CharacterNotFoundError):
        catalog.load_standing_image("unknown", "default")
