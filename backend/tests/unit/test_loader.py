import json
from pathlib import Path
from types import MappingProxyType

import pytest


def _valid_card() -> dict[str, object]:
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "unknown_root": "accepted",
        "data": {
            "name": "テスト人格",
            "description": "概要",
            "personality": "性格と話し方",
            "scenario": "関係と世界観",
            "first_mes": "初回表示",
            "mes_example": "会話例",
            "creator_notes": "作者メモ",
            "system_prompt": "常時指示",
            "post_history_instructions": "最終指示",
            "alternate_greetings": ["挨拶A", "挨拶B"],
            "group_only_greetings": ["グループ挨拶"],
            "creator": "作者",
            "character_version": "1.2.3",
            "unknown_data": "accepted",
            "extensions": {
                "digital_souls": {
                    "tts_config": {
                        "engine": "voicevox",
                        "speaker_id": 14,
                        "speaker_name": "冥鳴ひまり",
                    }
                },
                "future_namespace": {"enabled": True},
            },
        },
    }


def _write_card(
    root: Path,
    card: object,
    character: str = "testchar",
) -> None:
    character_dir = root / "characters" / character
    character_dir.mkdir(parents=True)
    (character_dir / f"{character}.card.json").write_text(
        json.dumps(card, ensure_ascii=False),
        encoding="utf-8",
    )


def _use_repo_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    import app.characters.loader as loader

    monkeypatch.setattr(loader, "_get_repo_root", lambda: root)


class TestLoadCharacterCardV3:
    def test_loads_all_supported_v3_fields(self, tmp_path, monkeypatch):
        _write_card(tmp_path, _valid_card())
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        card = load_character_card("testchar")

        assert card.spec == "chara_card_v3"
        assert card.spec_version == "3.0"
        assert card.data.name == "テスト人格"
        assert card.data.description == "概要"
        assert card.data.personality == "性格と話し方"
        assert card.data.scenario == "関係と世界観"
        assert card.data.first_mes == "初回表示"
        assert card.data.mes_example == "会話例"
        assert card.data.creator_notes == "作者メモ"
        assert card.data.system_prompt == "常時指示"
        assert card.data.post_history_instructions == "最終指示"
        assert card.data.alternate_greetings == ("挨拶A", "挨拶B")
        assert card.data.group_only_greetings == ("グループ挨拶",)
        assert card.data.creator == "作者"
        assert card.data.character_version == "1.2.3"

    def test_preserves_unknown_extension_namespaces(self, tmp_path, monkeypatch):
        _write_card(tmp_path, _valid_card())
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        card = load_character_card("testchar")

        assert card.data.extensions["future_namespace"] == {"enabled": True}

    def test_recursively_freezes_extension_values(self, tmp_path, monkeypatch):
        _write_card(tmp_path, _valid_card())
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        extensions = load_character_card("testchar").data.extensions
        digital_souls = extensions["digital_souls"]

        assert isinstance(extensions, MappingProxyType)
        assert isinstance(digital_souls, MappingProxyType)
        with pytest.raises(TypeError):
            digital_souls["tts_config"] = {}

    def test_accepts_unknown_root_and_data_fields(self, tmp_path, monkeypatch):
        _write_card(tmp_path, _valid_card())
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        card = load_character_card("testchar")

        assert card.data.name == "テスト人格"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("spec", "chara_card_v4"),
            ("spec_version", "4.0"),
        ],
    )
    def test_rejects_unsupported_card_identity(
        self, tmp_path, monkeypatch, field, value
    ):
        card = _valid_card()
        card[field] = value
        _write_card(tmp_path, card)
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        with pytest.raises(ValueError, match=field):
            load_character_card("testchar")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("description", []),
            ("alternate_greetings", ["valid", 1]),
            ("extensions", []),
        ],
    )
    def test_rejects_invalid_known_field_types(
        self, tmp_path, monkeypatch, field, value
    ):
        card = _valid_card()
        data = card["data"]
        assert isinstance(data, dict)
        data[field] = value
        _write_card(tmp_path, card)
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        with pytest.raises(ValueError, match=field):
            load_character_card("testchar")

    def test_rejects_path_traversal(self, tmp_path, monkeypatch):
        outside = tmp_path / "secrets"
        outside.mkdir()
        (outside / "secrets.card.json").write_text("{}", encoding="utf-8")
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        with pytest.raises(FileNotFoundError):
            load_character_card("../secrets")

    def test_raises_file_not_found_for_missing_card(self, tmp_path, monkeypatch):
        (tmp_path / "characters" / "missing").mkdir(parents=True)
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_character_card

        with pytest.raises(FileNotFoundError):
            load_character_card("missing")


class TestLoadTtsConfig:
    def test_loads_voicevox_config_from_digital_souls_extension(
        self, tmp_path, monkeypatch
    ):
        _write_card(tmp_path, _valid_card())
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.models import VoicevoxTtsConfig
        from app.characters.loader import load_tts_config

        result = load_tts_config("testchar")

        assert result == VoicevoxTtsConfig(speaker_id=14)

    @pytest.mark.parametrize(
        "tts_config",
        [
            {"engine": "other", "speaker_id": 14},
            {"engine": "voicevox", "speaker_id": True},
            {"engine": "voicevox", "speaker_id": "14"},
            {"engine": "voicevox"},
        ],
    )
    def test_rejects_invalid_voicevox_config(
        self, tmp_path, monkeypatch, tts_config
    ):
        card = _valid_card()
        data = card["data"]
        assert isinstance(data, dict)
        extensions = data["extensions"]
        assert isinstance(extensions, dict)
        digital_souls = extensions["digital_souls"]
        assert isinstance(digital_souls, dict)
        digital_souls["tts_config"] = tts_config
        _write_card(tmp_path, card)
        _use_repo_root(monkeypatch, tmp_path)
        from app.characters.loader import load_tts_config

        with pytest.raises(ValueError, match="tts_config"):
            load_tts_config("testchar")

    def test_repository_card_uses_v3_extension_contract(self):
        from app.characters.loader import load_character_card, load_tts_config

        card = load_character_card("miori")

        assert card.spec == "chara_card_v3"
        assert card.spec_version == "3.0"
        assert card.data.creator == ""
        assert card.data.character_version == ""
        assert card.data.creator_notes == ""
        assert card.data.alternate_greetings == ()
        assert card.data.group_only_greetings == ()
        assert load_tts_config("miori").speaker_id == 14
