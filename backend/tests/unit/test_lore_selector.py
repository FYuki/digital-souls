from dataclasses import replace

import pytest

from app.characters.lore_selector import (
    CharacterLoreSelection,
    LoreActivationKind,
    LoreSelectionReason,
    SelectedCharacterLore,
    select_character_lore,
)
from app.characters.models import (
    CharacterBook,
    CharacterBookEntry,
    CharacterLorePosition,
)


class ContentLengthTokenCounter:
    def count_lore_tokens(
        self,
        entries: tuple[SelectedCharacterLore, ...],
    ) -> int:
        return sum(len(entry.content) for entry in entries)


def _entry(**overrides: object) -> CharacterBookEntry:
    entry = CharacterBookEntry(
        keys=("由来",),
        content="Character Lore",
        extensions={},
        enabled=True,
        insertion_order=10,
        use_regex=False,
        case_sensitive=None,
        constant=None,
        name=None,
        priority=None,
        id=None,
        comment=None,
        selective=None,
        secondary_keys=None,
        position=None,
        extra_fields={},
    )
    return replace(entry, **overrides)  # type: ignore[arg-type]


def _book(
    *entries: CharacterBookEntry,
    **overrides: object,
) -> CharacterBook:
    book = CharacterBook(
        name=None,
        description=None,
        scan_depth=None,
        token_budget=None,
        recursive_scanning=None,
        extensions={},
        entries=entries,
        extra_fields={},
    )
    return replace(book, **overrides)  # type: ignore[arg-type]


def _select(
    book: CharacterBook | None,
    *messages_newest_first: str,
) -> CharacterLoreSelection:
    return select_character_lore(
        book,
        messages_newest_first,
        ContentLengthTokenCounter(),
    )


def _reasons(selection: CharacterLoreSelection) -> tuple[LoreSelectionReason, ...]:
    return tuple(decision.reason for decision in selection.decisions)


def test_no_book_returns_empty_selection() -> None:
    selection = _select(None, "由来")

    assert selection.entries == ()
    assert selection.decisions == ()
    assert selection.lore_budget_tokens is None
    assert selection.recursive_scanning_ignored is False


def test_default_scan_depth_only_scans_current_message() -> None:
    selection = _select(_book(_entry()), "現在の質問", "由来を教えて")

    assert selection.entries == ()
    assert _reasons(selection) == (LoreSelectionReason.PRIMARY_KEY_MISS,)


def test_scan_depth_counts_individual_messages_from_newest() -> None:
    book = _book(_entry(), scan_depth=2)

    assert len(_select(book, "現在", "由来を教えて", "さらに古い").entries) == 1
    assert _select(book, "現在", "直前", "由来を教えて").entries == ()


def test_scan_depth_zero_does_not_scan_messages() -> None:
    selection = _select(_book(_entry(), scan_depth=0), "由来")

    assert selection.entries == ()


def test_matching_uses_nfkc_and_is_case_insensitive_by_default() -> None:
    entry = _entry(keys=("ＡＢＣ",))

    selection = _select(_book(entry), "abcについて")

    assert selection.entries[0].activation_kind is LoreActivationKind.PRIMARY
    assert _reasons(selection) == (LoreSelectionReason.SELECTED_PRIMARY,)


def test_case_sensitive_matching_can_miss() -> None:
    entry = _entry(keys=("ABC",), case_sensitive=True)

    selection = _select(_book(entry), "abcについて")

    assert selection.entries == ()
    assert _reasons(selection) == (LoreSelectionReason.PRIMARY_KEY_MISS,)


def test_key_does_not_match_across_message_boundaries() -> None:
    entry = _entry(keys=("前半後半",))

    selection = _select(_book(entry, scan_depth=2), "後半", "前半")

    assert selection.entries == ()


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (_entry(enabled=False), LoreSelectionReason.DISABLED),
        (_entry(content=" \n "), LoreSelectionReason.EMPTY_CONTENT),
        (
            _entry(content="本文\n  @@depth 2\n続き"),
            LoreSelectionReason.UNSUPPORTED_DECORATOR,
        ),
        (_entry(use_regex=True), LoreSelectionReason.UNSUPPORTED_REGEX),
        (_entry(keys=("", "   ")), LoreSelectionReason.PRIMARY_KEY_MISS),
    ],
)
def test_non_selectable_entry_records_content_free_reason(
    entry: CharacterBookEntry,
    reason: LoreSelectionReason,
) -> None:
    selection = _select(_book(entry), "由来")

    assert selection.entries == ()
    assert _reasons(selection) == (reason,)
    assert "Character Lore" not in repr(selection)


def test_constant_entry_does_not_require_keys() -> None:
    entry = _entry(keys=(), constant=True)

    selection = _select(_book(entry), "無関係")

    assert selection.entries[0].activation_kind is LoreActivationKind.CONSTANT
    assert _reasons(selection) == (LoreSelectionReason.SELECTED_CONSTANT,)


def test_unsupported_regex_is_skipped_even_when_constant() -> None:
    entry = _entry(keys=(), constant=True, use_regex=True)

    selection = _select(_book(entry), "無関係")

    assert selection.entries == ()
    assert _reasons(selection) == (LoreSelectionReason.UNSUPPORTED_REGEX,)


def test_selective_entry_requires_primary_and_secondary_keys() -> None:
    entry = _entry(
        keys=("海",),
        selective=True,
        secondary_keys=("月",),
    )
    book = _book(entry, scan_depth=2)

    selected = _select(book, "海の話", "月の話")
    missed = _select(book, "海の話", "星の話")

    assert selected.entries[0].activation_kind is LoreActivationKind.SELECTIVE
    assert _reasons(selected) == (LoreSelectionReason.SELECTED_SELECTIVE,)
    assert missed.entries == ()
    assert _reasons(missed) == (LoreSelectionReason.SECONDARY_KEY_MISS,)


def test_selective_entry_without_secondary_keys_is_not_selected() -> None:
    entry = _entry(keys=("海",), selective=True, secondary_keys=None)

    selection = _select(_book(entry), "海の話")

    assert selection.entries == ()
    assert _reasons(selection) == (LoreSelectionReason.SECONDARY_KEY_MISS,)


def test_each_entry_is_selected_only_once_for_multiple_matches() -> None:
    entry = _entry(keys=("海", "月"))

    selection = _select(_book(entry, scan_depth=2), "海と月", "海")

    assert len(selection.entries) == 1


def test_prompt_order_uses_position_then_insertion_order_then_source_index() -> None:
    book = _book(
        _entry(content="after", insertion_order=0),
        _entry(
            content="before-later",
            insertion_order=20,
            position=CharacterLorePosition.BEFORE_CHAR,
        ),
        _entry(
            content="before-earlier-a",
            insertion_order=10,
            position=CharacterLorePosition.BEFORE_CHAR,
        ),
        _entry(
            content="before-earlier-b",
            insertion_order=10,
            position=CharacterLorePosition.BEFORE_CHAR,
        ),
    )

    selection = _select(book, "由来")

    assert tuple(entry.content for entry in selection.entries) == (
        "before-earlier-a",
        "before-earlier-b",
        "before-later",
        "after",
    )
    assert selection.entries[-1].position is CharacterLorePosition.AFTER_CHAR


def test_lore_budget_removes_lower_priority_before_higher_priority() -> None:
    book = _book(
        _entry(content="low", priority=1),
        _entry(content="higher", priority=2),
        token_budget=6,
    )

    selection = _select(book, "由来")

    assert tuple(entry.content for entry in selection.entries) == ("higher",)
    assert _reasons(selection) == (
        LoreSelectionReason.OMITTED_BY_LORE_BUDGET,
        LoreSelectionReason.SELECTED_PRIMARY,
    )
    assert selection.lore_budget_tokens == 6
    assert selection.omitted_by_budget == 1


def test_lore_budget_tie_removes_lower_order_then_earlier_source() -> None:
    book = _book(
        _entry(content="first", priority=1, insertion_order=10),
        _entry(content="second", priority=1, insertion_order=10),
        _entry(content="third", priority=1, insertion_order=20),
        token_budget=11,
    )

    selection = _select(book, "由来")

    assert tuple(entry.content for entry in selection.entries) == (
        "second",
        "third",
    )
    assert _reasons(selection) == (
        LoreSelectionReason.OMITTED_BY_LORE_BUDGET,
        LoreSelectionReason.SELECTED_PRIMARY,
        LoreSelectionReason.SELECTED_PRIMARY,
    )


def test_zero_lore_budget_removes_all_nonempty_entries() -> None:
    selection = _select(
        _book(_entry(content="a"), _entry(content="b"), token_budget=0),
        "由来",
    )

    assert selection.entries == ()
    assert selection.lore_budget_tokens == 0
    assert selection.omitted_by_budget == 2


def test_recursive_scanning_is_reported_but_does_not_activate_other_entries() -> None:
    book = _book(
        _entry(keys=(), content="月", constant=True),
        _entry(keys=("月",), content="月のLore"),
        recursive_scanning=True,
    )

    selection = _select(book, "無関係")

    assert tuple(entry.content for entry in selection.entries) == ("月",)
    assert selection.recursive_scanning_ignored is True


@pytest.mark.parametrize("invalid_count", [-1, True, 1.5])
def test_invalid_token_counter_result_is_rejected(invalid_count: object) -> None:
    class InvalidCounter:
        def count_lore_tokens(
            self,
            entries: tuple[SelectedCharacterLore, ...],
        ) -> int:
            del entries
            return invalid_count  # type: ignore[return-value]

    with pytest.raises(
        ValueError,
        match="character lore token count must be a non-negative integer",
    ):
        select_character_lore(
            _book(_entry(), token_budget=1),
            ("由来",),
            InvalidCounter(),
        )
