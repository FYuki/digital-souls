from pathlib import Path

import pytest

from tests.character_card_test_support import (
    character_book,
    character_book_entry,
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

    def test_should_not_expose_character_lore_in_repr(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        secret = "SECRET_CHARACTER_LORE_74"
        book = character_book(entries=[character_book_entry(content=secret)])
        data = character_card_data(character_book=book)
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import load_character_card

        loaded = load_character_card("test").data.character_book

        assert loaded is not None
        assert secret not in repr(loaded)
        assert secret not in repr(loaded.entries[0])

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

    def test_should_load_all_character_book_fields_and_preserve_unknown_fields(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = character_book_entry(
            keys=["由来", "誕生"],
            content="Lore本文",
            extensions={"future_entry_extension": {"revision": 1}},
            enabled=True,
            insertion_order=-4,
            use_regex=True,
            case_sensitive=True,
            constant=True,
            name="由来",
            priority=-2,
            id="origin",
            comment="編集用注記",
            selective=True,
            secondary_keys=["光"],
            position="before_char",
            future_entry={"enabled": True},
        )
        book = character_book(
            name="合成Lorebook",
            description="編集用説明",
            scan_depth=0,
            token_budget=0,
            recursive_scanning=True,
            extensions={"future_book_extension": {"revision": 2}},
            entries=[entry],
            future_book={"enabled": False},
        )
        data = character_card_data(character_book=book)
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import load_character_card
        from app.characters.models import CharacterLorePosition

        loaded = load_character_card("test").data.character_book

        assert loaded is not None
        assert loaded.name == "合成Lorebook"
        assert loaded.description == "編集用説明"
        assert loaded.scan_depth == 0
        assert loaded.token_budget == 0
        assert loaded.recursive_scanning is True
        assert loaded.extensions == {"future_book_extension": {"revision": 2}}
        assert loaded.extra_fields == {"future_book": {"enabled": False}}
        assert len(loaded.entries) == 1
        loaded_entry = loaded.entries[0]
        assert loaded_entry.keys == ("由来", "誕生")
        assert loaded_entry.content == "Lore本文"
        assert loaded_entry.extensions == {
            "future_entry_extension": {"revision": 1}
        }
        assert loaded_entry.enabled is True
        assert loaded_entry.insertion_order == -4
        assert loaded_entry.use_regex is True
        assert loaded_entry.case_sensitive is True
        assert loaded_entry.constant is True
        assert loaded_entry.name == "由来"
        assert loaded_entry.priority == -2
        assert loaded_entry.id == "origin"
        assert loaded_entry.comment == "編集用注記"
        assert loaded_entry.selective is True
        assert loaded_entry.secondary_keys == ("光",)
        assert loaded_entry.position is CharacterLorePosition.BEFORE_CHAR
        assert loaded_entry.extra_fields == {
            "future_entry": {"enabled": True}
        }

    def test_should_apply_character_book_optional_defaults(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = character_card_data(character_book=character_book())
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import load_character_card

        loaded = load_character_card("test").data.character_book

        assert loaded is not None
        assert loaded.name is None
        assert loaded.description is None
        assert loaded.scan_depth is None
        assert loaded.token_budget is None
        assert loaded.recursive_scanning is None
        entry = loaded.entries[0]
        assert entry.case_sensitive is None
        assert entry.constant is None
        assert entry.name is None
        assert entry.priority is None
        assert entry.id is None
        assert entry.comment is None
        assert entry.selective is None
        assert entry.secondary_keys is None
        assert entry.position is None

    def test_should_keep_character_book_absence_distinct_from_empty_book(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_character_card(tmp_path, "test", character_card_document())
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import load_character_card

        card = load_character_card("test")

        assert card.data.character_book is None
        assert "character_book" not in card.data.extra_fields

    @pytest.mark.parametrize(
        ("book", "path"),
        [
            (None, "data.character_book"),
            ({}, "data.character_book.entries"),
            (
                {"entries": [], "extensions": []},
                "data.character_book.extensions",
            ),
            (
                {"entries": [], "extensions": {}, "scan_depth": -1},
                "data.character_book.scan_depth",
            ),
            (
                {"entries": [], "extensions": {}, "token_budget": True},
                "data.character_book.token_budget",
            ),
        ],
    )
    def test_should_reject_malformed_character_book_with_field_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        book: object,
        path: str,
    ) -> None:
        data = character_card_data(character_book=book)
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import CharacterCardValidationError
        from app.characters.loader import load_character_card

        with pytest.raises(CharacterCardValidationError, match=path.replace("[", r"\[").replace("]", r"\]")):
            load_character_card("test")

    @pytest.mark.parametrize(
        ("entry", "path"),
        [
            (None, "data.character_book.entries[0]"),
            ({}, "data.character_book.entries[0].keys"),
            (
                character_book_entry(keys=["由来", 4]),
                "data.character_book.entries[0].keys",
            ),
            (
                character_book_entry(enabled=1),
                "data.character_book.entries[0].enabled",
            ),
            (
                character_book_entry(insertion_order=1.5),
                "data.character_book.entries[0].insertion_order",
            ),
            (
                character_book_entry(use_regex="false"),
                "data.character_book.entries[0].use_regex",
            ),
            (
                character_book_entry(id=True),
                "data.character_book.entries[0].id",
            ),
            (
                character_book_entry(position="middle"),
                "data.character_book.entries[0].position",
            ),
        ],
    )
    def test_should_reject_malformed_character_book_entry_with_field_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        entry: object,
        path: str,
    ) -> None:
        data = character_card_data(
            character_book=character_book(entries=[entry])
        )
        write_character_card(tmp_path, "test", character_card_document(data=data))
        use_character_repo_root(monkeypatch, tmp_path)

        from app.characters.loader import CharacterCardValidationError
        from app.characters.loader import load_character_card

        pattern = path.replace("[", r"\[").replace("]", r"\]")
        with pytest.raises(CharacterCardValidationError, match=pattern):
            load_character_card("test")

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
