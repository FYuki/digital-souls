import json
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, cast

import pytest


class _ForwardCompatibleCardData(Protocol):
    extra_fields: Mapping[str, object]


class _ForwardCompatibleCard(Protocol):
    spec_version: str
    data: _ForwardCompatibleCardData
    extra_fields: Mapping[str, object]


def _future_compatible_card() -> dict[str, object]:
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "unknown_root": {
            "future_flag": True,
            "nested_items": [{"label": "root-value"}],
        },
        "data": {
            "name": "テスト人格",
            "description": "概要",
            "personality": "性格",
            "scenario": "シナリオ",
            "first_mes": "初回表示",
            "mes_example": "会話例",
            "creator_notes": "作者メモ",
            "system_prompt": "常時指示",
            "post_history_instructions": "最終指示",
            "alternate_greetings": [],
            "group_only_greetings": [],
            "creator": "作者",
            "character_version": "1.0",
            "tags": ["相棒"],
            "unknown_data": {
                "future_flag": True,
                "nested_items": [{"label": "data-value"}],
            },
            "extensions": {},
        },
    }


def _write_card(root: Path, card: object) -> None:
    character_dir = root / "characters" / "testchar"
    character_dir.mkdir(parents=True)
    character_dir.joinpath("testchar.card.json").write_text(
        json.dumps(card, ensure_ascii=False),
        encoding="utf-8",
    )


def _use_repo_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    import app.characters.loader as loader

    monkeypatch.setattr(loader, "_get_repo_root", lambda: root)


class TestCharacterCardUnknownFields:
    def test_preserves_unknown_root_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_card(tmp_path, _future_compatible_card())
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        card = cast(_ForwardCompatibleCard, load_character_card("testchar"))

        assert card.extra_fields["unknown_root"] == {
            "future_flag": True,
            "nested_items": ({"label": "root-value"},),
        }

    def test_preserves_unknown_data_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_card(tmp_path, _future_compatible_card())
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        card = cast(_ForwardCompatibleCard, load_character_card("testchar"))

        assert card.data.extra_fields["unknown_data"] == {
            "future_flag": True,
            "nested_items": ({"label": "data-value"},),
        }

    @pytest.mark.parametrize("level", ["root", "data"])
    def test_recursively_freezes_unknown_fields(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        level: Literal["root", "data"],
    ) -> None:
        _write_card(tmp_path, _future_compatible_card())
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        card = cast(_ForwardCompatibleCard, load_character_card("testchar"))
        if level == "root":
            extra_fields = card.extra_fields
            unknown = extra_fields["unknown_root"]
        else:
            extra_fields = card.data.extra_fields
            unknown = extra_fields["unknown_data"]

        assert isinstance(extra_fields, MappingProxyType)
        assert isinstance(unknown, MappingProxyType)
        assert isinstance(unknown["nested_items"], tuple)
        with pytest.raises(TypeError):
            cast(MutableMapping[str, object], unknown)["future_flag"] = False


class TestCharacterCardVersion:
    @pytest.mark.parametrize("spec_version", ["3.1", "4.0", "10.2"])
    def test_rejects_future_versions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        spec_version: str,
    ) -> None:
        card = _future_compatible_card()
        card["spec_version"] = spec_version
        _write_card(tmp_path, card)
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        with pytest.raises(ValueError, match="spec_version must be '3.0'"):
            load_character_card("testchar")

    @pytest.mark.parametrize("spec_version", ["2.9", "not-a-version", "NaN", "Infinity"])
    def test_rejects_old_or_invalid_versions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        spec_version: str,
    ) -> None:
        card = _future_compatible_card()
        card["spec_version"] = spec_version
        _write_card(tmp_path, card)
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        with pytest.raises(ValueError, match="spec_version"):
            load_character_card("testchar")
