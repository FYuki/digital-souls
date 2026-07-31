from pathlib import Path

import pytest

from tests.character_card_test_support import (
    character_card_data,
    character_card_document,
    use_character_repo_root,
    write_character_card,
)


class TestLoadCharacterCard:
    def test_should_not_expose_card_bodies_or_unknown_fields_in_repr(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        secret_values = (
            "SECRET_CARD_BODY_71",
            "SECRET_DATA_UNKNOWN_72",
            "SECRET_ROOT_UNKNOWN_73",
        )
        data = character_card_data(
            description=secret_values[0],
            future_data=secret_values[1],
        )
        document = character_card_document(
            data=data,
            future_root=secret_values[2],
        )
        write_character_card(tmp_path, "test", document)
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import load_character_card

        card = load_character_card("test")

        card_values = (card.data, card)
        assert all(
            secret not in repr(value)
            for value in card_values
            for secret in secret_values
        )

    def test_should_load_all_supported_v3_fields(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = character_card_data(
            creator="作成者",
            character_version="2.1",
            creator_notes="作成者向け注記",
            alternate_greetings=["挨拶A", "挨拶B"],
            group_only_greetings=["グループ挨拶"],
        )
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import load_character_card

        card = load_character_card("test")

        assert card.spec == "chara_card_v3"
        assert card.spec_version == "3.0"
        assert card.data.creator == "作成者"
        assert card.data.character_version == "2.1"
        assert card.data.creator_notes == "作成者向け注記"
        assert card.data.alternate_greetings == ("挨拶A", "挨拶B")
        assert card.data.group_only_greetings == ("グループ挨拶",)
        assert card.data.extensions["digital_souls"]["tts_config"]["speaker_id"] == 14

    def test_should_apply_v3_defaults_when_optional_fields_are_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = character_card_data()
        for field in (
            "creator",
            "character_version",
            "creator_notes",
            "post_history_instructions",
            "alternate_greetings",
            "group_only_greetings",
            "tags",
            "extensions",
        ):
            data.pop(field)
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import load_character_card

        card = load_character_card("test")

        assert card.data.creator == ""
        assert card.data.character_version == ""
        assert card.data.creator_notes == ""
        assert card.data.post_history_instructions == ""
        assert card.data.alternate_greetings == ()
        assert card.data.group_only_greetings == ()
        assert card.data.tags == ()
        assert card.data.extensions == {}

    def test_should_preserve_unknown_fields_without_reinterpreting_them(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = character_card_data(future_data={"enabled": True})
        document = character_card_document(
            data=data,
            future_root={"revision": 4},
        )
        write_character_card(tmp_path, "test", document)
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import load_character_card

        card = load_character_card("test")

        assert card.extra_fields == {"future_root": {"revision": 4}}
        assert card.data.extra_fields == {"future_data": {"enabled": True}}

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("spec", "chara_card_v2"),
            ("spec", "CHARA_CARD_V3"),
            ("spec_version", "3.1"),
            ("spec_version", 3.0),
        ],
    )
    def test_should_reject_unsupported_card_identity(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
        value: object,
    ) -> None:
        document = character_card_document()
        document[field] = value
        write_character_card(tmp_path, "test", document)
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import UnsupportedCharacterCardError
        from app.characters.loader import load_character_card

        with pytest.raises(UnsupportedCharacterCardError) as captured:
            load_character_card("test")

        assert captured.value.field == field
        assert captured.value.actual == value

    @pytest.mark.parametrize(
        "data",
        [None, [], "invalid"],
    )
    def test_should_reject_non_object_card_data(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        data: object,
    ) -> None:
        write_character_card(
            tmp_path,
            "test",
            {
                "spec": "chara_card_v3",
                "spec_version": "3.0",
                "data": data,
            },
        )
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import CharacterCardValidationError
        from app.characters.loader import load_character_card

        with pytest.raises(CharacterCardValidationError):
            load_character_card("test")

    @pytest.mark.parametrize(
        "document",
        [
            [],
            {"spec_version": "3.0", "data": character_card_data()},
            {"spec": "chara_card_v3", "data": character_card_data()},
        ],
    )
    def test_should_reject_invalid_root_contract(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        document: object,
    ) -> None:
        write_character_card(tmp_path, "test", document)
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import CharacterCardValidationError
        from app.characters.loader import load_character_card

        with pytest.raises(CharacterCardValidationError):
            load_character_card("test")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("alternate_greetings", {}),
            ("alternate_greetings", None),
            ("group_only_greetings", "挨拶"),
            ("tags", {}),
            ("post_history_instructions", []),
            ("extensions", []),
        ],
    )
    def test_should_reject_invalid_optional_field_shapes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
        value: object,
    ) -> None:
        data = character_card_data()
        data[field] = value
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import CharacterCardValidationError
        from app.characters.loader import load_character_card

        with pytest.raises(CharacterCardValidationError):
            load_character_card("test")

    def test_should_reject_path_traversal_outside_characters_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_character_card(tmp_path, "safe", character_card_document())
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import load_character_card

        with pytest.raises(FileNotFoundError):
            load_character_card("../safe")
