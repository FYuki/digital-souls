"""ADR 0028 synthetic contracts; Miori expectations pinned before implementation."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from digital_souls_core.character import load_characters
from digital_souls_core.character_book import CharacterCardValidationError, load_book, select_lore
from digital_souls_core.contracts import Message
from digital_souls_core.prompt_builder import (
    ByteTokenCounter,
    CharacterCard,
    PromptBuilder,
    PromptInput,
    split_messages,
)

from .support import ROOT, character
from .test_prompt_builder import UnitCounter, budgets

pytestmark = pytest.mark.ut


def entry(**changes: Any) -> dict[str, Any]:
    return (
        dict(
            keys=["key"],
            content="body",
            extensions={},
            enabled=True,
            insertion_order=0,
            use_regex=False,
            **{},
        )
        | changes
    )


def book(*entries: dict[str, Any], **changes: Any) -> Any:
    return load_book(dict(extensions={}, entries=list(entries)) | changes)


async def select(
    *entries: dict[str, Any],
    current: str = "key",
    history: tuple[Message, ...] = (),
    **changes: Any,
) -> Any:
    return await select_lore(book(*entries, **changes), current, history, ByteTokenCounter())


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"enabled": False, "use_regex": True}, "disabled"),
        ({"content": "  ", "use_regex": True}, "empty_content"),
        (
            {"content": "body\n  @@@rule", "use_regex": True, "constant": True},
            "unsupported_decorator",
        ),
        ({"use_regex": True, "constant": True}, "unsupported_regex"),
        ({"keys": ["absent"]}, "primary_key_miss"),
        ({"constant": True, "selective": True}, "selected_constant"),
        ({}, "selected_primary"),
        ({"selective": True, "secondary_keys": ["key"]}, "selected_selective"),
        ({"selective": True}, "secondary_key_miss"),
        ({"selective": True, "secondary_keys": []}, "secondary_key_miss"),
        ({"selective": True, "secondary_keys": [""]}, "secondary_key_miss"),
    ],
)
async def test_activation_order(changes: dict[str, Any], reason: str) -> None:
    result = await select(entry(**changes))
    assert result.diagnostics[0].reason == reason
    assert bool(result.entries) == reason.startswith("selected_")


@pytest.mark.parametrize(
    ("key", "text", "sensitive", "hit"),
    [
        ("ＡＢＣ", "abc", False, True),
        ("ＡＢＣ", "ABC", True, True),
        ("ABC", "abc", True, False),
        ("Straße", "STRASSE", False, True),
        ("", "abc", False, False),
        ("a\nb", "a\nb", False, True),
    ],
)
async def test_literal_normalization(key: str, text: str, sensitive: bool, hit: bool) -> None:
    result = await select(entry(keys=[key], case_sensitive=sensitive), current=text)
    assert bool(result.entries) == hit


@pytest.mark.parametrize(
    ("depth", "hit"), [(0, False), (1, False), (2, False), (3, True), (99, True)]
)
async def test_scan_message_count_and_boundary(depth: int, hit: bool) -> None:
    history = (Message(role="user", content="key"), Message(role="assistant", content="tail"))
    result = await select(entry(), current="now", history=history, scan_depth=depth)
    assert bool(result.entries) == hit
    assert not (
        await select(entry(keys=["keytail"]), current="now", history=history, scan_depth=99)
    ).entries
    assert not (await select(entry(), current="now", history=history)).entries
    assert (await select(entry(constant=True), current="now", scan_depth=0)).entries


async def test_dedupe_uses_source_index_and_recursive_content_is_never_scanned() -> None:
    result = await select(
        entry(keys=["key", "ey"], id="same", content="next"),
        entry(keys=["key"], id="same"),
        entry(keys=["next"]),
        recursive_scanning=True,
    )
    assert [e.source_index for e in result.entries] == [0, 1]


async def test_tool_and_system_messages_are_not_scanned_or_counted() -> None:
    history = (
        Message(role="user", content="key"),
        Message(role="system", content="secret"),
        Message(role="tool", content="secret", tool_call_id="id"),
    )
    assert (await select(entry(), current="now", history=history, scan_depth=2)).entries
    assert not (
        await select(entry(keys=["secret"]), current="now", history=history, scan_depth=99)
    ).entries


async def test_book_none_empty_and_budget_boundaries() -> None:
    assert not (await select_lore(None, "key", (), ByteTokenCounter())).entries
    assert not (await select()).entries
    cost = (await select(entry())).tokens
    for limit, count in [(0, 0), (cost - 1, 0), (cost, 1), (cost + 1, 1)]:
        result = await select(entry(), token_budget=limit)
        assert len(result.entries) == count
        if not count:
            assert result.diagnostics[0].reason == "omitted_by_lore_budget"
    assert len((await select(entry(), entry(), token_budget=cost)).entries) == 1
    assert not (await select(entry(), entry(), token_budget=1)).entries


async def test_budget_priority_order_and_source_index_ties() -> None:
    entries = (
        entry(insertion_order=3, priority=-1),
        entry(insertion_order=1, priority=7),
        entry(insertion_order=1, priority=7),
        entry(insertion_order=2),
    )
    result = await select(*entries)
    single = (await select(entry())).tokens
    retained = await select(*entries, token_budget=single)
    assert [e.source_index for e in retained.entries] == [2]
    assert [e.source_index for e in result.entries] == [1, 2, 3, 0]
    assert result.entries[0].removal_key == (7, 1, 1)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("keys", None),
        ("keys", ["ok", 1]),
        ("content", 2),
        ("extensions", []),
        ("enabled", 1),
        ("insertion_order", True),
        ("insertion_order", 1.5),
        ("use_regex", "false"),
        ("case_sensitive", 0),
        ("constant", "true"),
        ("name", []),
        ("priority", True),
        ("priority", 1.0),
        ("id", True),
        ("comment", {}),
        ("selective", 1),
        ("secondary_keys", ["ok", False]),
        ("position", "invalid-private-value"),
    ],
)
def test_invalid_entry_rejects_whole_card_with_safe_path(field: str, bad: object) -> None:
    with pytest.raises(CharacterCardValidationError) as caught:
        book(entry(), entry(), entry(**{field: bad}))
    assert f"data.character_book.entries[2].{field}" in str(caught.value)
    assert "invalid-private-value" not in repr(caught.value)
    assert "body" not in str(caught.value)


@pytest.mark.parametrize(
    "field", ["keys", "content", "extensions", "enabled", "insertion_order", "use_regex"]
)
def test_missing_required_entry_field(field: str) -> None:
    value = entry()
    del value[field]
    with pytest.raises(CharacterCardValidationError, match=field):
        book(value)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("scan_depth", -1),
        ("scan_depth", True),
        ("scan_depth", 1.5),
        ("token_budget", -1),
        ("token_budget", False),
        ("token_budget", "1"),
        ("recursive_scanning", 1),
        ("name", 4),
        ("description", []),
        ("entries", {}),
        ("extensions", []),
    ],
)
def test_invalid_book_fields(field: str, bad: object) -> None:
    with pytest.raises(CharacterCardValidationError, match="data.character_book." + field):
        book(**{field: bad})


def test_unknown_fields_lossless_and_nullable_optionals() -> None:
    raw = dict(
        extensions={"custom": [None, {"x": True}]},
        entries=[
            entry(
                name=None,
                id=None,
                priority=None,
                secondary_keys=None,
                position=None,
                future={"x": [1, None]},
            )
        ],
        future=["y"],
        scan_depth=None,
        token_budget=None,
    )
    loaded = load_book(raw)
    assert loaded is not None
    assert loaded.extensions == raw["extensions"]
    assert loaded.extra_fields == {"future": ["y"]}
    assert loaded.entries[0].extra_fields == {"future": {"x": [1, None]}}
    assert "body" not in repr(loaded)


async def test_before_after_heading_and_total_budget_reason() -> None:
    selected = await select(
        entry(content="after", insertion_order=2),
        entry(content="before", position="before_char", insertion_order=9, priority=-10),
    )
    data = PromptInput(card=CharacterCard(description="persona"), lore=selected.entries)
    built = await PromptBuilder(UnitCounter()).build(data, budgets())
    assert (
        built.messages[0].content == "## キャラクターLore\nbefore\n\n## キャラクター概要\npersona"
        "\n\n## キャラクターLore\nafter"
    )
    reduced = await PromptBuilder(UnitCounter()).build(data, budgets(total=2))
    assert "before" not in (reduced.messages[0].content or "")
    assert reduced.usage.lore_diagnostics[0].reason == "omitted_by_total_budget"
    assert reduced.usage.dropped["lore"] == 1


async def test_scan_precedes_history_pruning_and_ignores_other_regions() -> None:
    separated = split_messages(
        [
            Message(role="user", content="key"),
            Message(role="assistant", content="old"),
            Message(role="user", content="recent"),
            Message(role="assistant", content="answer"),
            Message(role="user", content="now"),
        ]
    )
    result = await select(
        entry(),
        current="now",
        history=tuple(m for t in separated.history for m in t.messages),
        scan_depth=99,
    )
    built = await PromptBuilder().build(
        PromptInput(
            lore=result.entries, history=separated.history, current_input=separated.current_input
        ),
        budgets(history=70),
    )
    assert built.usage.dropped["history"] == 1
    assert "キャラクターLore" in (built.messages[0].content or "")


async def test_loader_and_miori_pinned_regression() -> None:
    char = load_characters(ROOT / "examples/characters.json")[0]
    cases = json.loads(
        (ROOT / "tests/fixtures/miori-character-book-2026-10-11-origin-lore.json").read_text()
    )
    for case in cases:
        result = await select_lore(char.character_book, case["question"], (), ByteTokenCounter())
        if case["estimate_tokens"] <= 700:
            assert [e.source_index for e in result.entries] == case["source_indices"]
            assert result.tokens == case["estimate_tokens"]
        else:
            assert case["estimate_tokens"] == 2436
            assert [e.source_index for e in result.entries] == [0, 4]
            assert result.tokens == 635
            assert sum(d.reason == "omitted_by_lore_budget" for d in result.diagnostics) == 5


def test_loader_rejects_invalid_book_including_disabled_entry(tmp_path: Path) -> None:
    config = character("synthetic").config.model_dump(mode="json", exclude_none=True) | {
        "card_path": "card.json"
    }
    (tmp_path / "config.json").write_text(json.dumps([config]))
    (tmp_path / "card.json").write_text(
        json.dumps(
            {
                "spec": "chara_card_v3",
                "spec_version": "3.0",
                "data": {
                    "description": "persona",
                    "character_book": {"extensions": {}, "entries": [entry(enabled=False, keys=1)]},
                },
            }
        )
    )
    with pytest.raises(
        CharacterCardValidationError, match=r"data.character_book.entries\[0\].keys"
    ):
        load_characters(tmp_path / "config.json")


async def test_empty_book_or_no_match_is_identical_to_no_book() -> None:
    data = PromptInput(
        card=CharacterCard(description="persona", post_history_instructions="post"),
        current_input=(Message(role="user", content="now"),),
    )
    expected = await PromptBuilder().build(data, budgets())
    for value in (None, book(), book(entry())):
        selection = await select_lore(value, "now", (), ByteTokenCounter())
        actual = await PromptBuilder().build(replace(data, lore=selection.entries), budgets())
        assert actual.messages == expected.messages
        assert actual.usage == expected.usage


async def test_async_counter_measures_all_lore_messages_and_recounts_survivors() -> None:
    measured: list[tuple[Message, ...]] = []

    async def counter(messages: Any, *, tools: Any = ()) -> int:
        assert not tools
        measured.append(tuple(messages))
        assert all(
            m.role == "system" and m.content.startswith("## キャラクターLore\n") for m in messages
        )
        return len(messages) * 10

    result = await select_lore(
        book(
            entry(content="after"), entry(content="before", position="before_char"), token_budget=10
        ),
        "key",
        (),
        counter,
    )
    assert [len(messages) for messages in measured] == [2, 1]
    assert [e.source_index for e in result.entries] == [1]
    assert result.tokens == 10


async def test_regional_lore_budget_reason_is_distinct_from_total_budget() -> None:
    selection = await select(
        entry(content="before", position="before_char"), entry(content="after")
    )
    built = await PromptBuilder(UnitCounter()).build(
        PromptInput(lore=selection.entries), budgets(lore=1)
    )
    assert built.usage.lore_diagnostics[0].reason == "omitted_by_lore_budget"


async def test_selector_and_diagnostics_are_silent_and_hide_all_card_data(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    private = "synthetic-private-marker"
    loaded = book(
        entry(
            keys=[private],
            secondary_keys=[private],
            name=private,
            comment=private,
            id=private,
            content=private,
            extensions={"private": private},
            future=private,
        ),
        name=private,
        description=private,
        extensions={"private": private},
        future=private,
        token_budget=0,
    )
    result = await select_lore(loaded, private, (), ByteTokenCounter())
    assert not result.entries
    assert private not in repr(result) and private not in repr(result.diagnostics)
    assert private not in repr(loaded) and private not in repr(loaded.entries[0])
    assert not caplog.records and capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "content,hit",
    [
        ("@@rule\nbody", False),
        (" \t@@rule", False),
        ("body @@rule", True),
        ("body\n@@@rule", False),
    ],
)
async def test_decorator_detection_is_line_based(content: str, hit: bool) -> None:
    assert bool((await select(entry(content=content))).entries) == hit


async def test_selective_keys_can_match_different_messages_without_mutating_sources() -> None:
    loaded = book(entry(keys=["ＡＢＣ"], secondary_keys=["Straße"], selective=True), scan_depth=2)
    history = (Message(role="assistant", content="STRASSE"),)
    selected = await select_lore(loaded, "abc", history, ByteTokenCounter())
    assert len(selected.entries) == 1
    assert selected.entries[0].activation_kind == "selective"
    assert loaded.entries[0].keys == ("ＡＢＣ",)
    assert loaded.entries[0].secondary_keys == ("Straße",)
    assert history[0].content == "STRASSE"


@pytest.mark.parametrize("position", [None, "before_char", "after_char"])
async def test_position_resolution(position: str | None) -> None:
    selected = await select(entry(position=position))
    assert selected.entries[0].position == (position or "after_char")


async def test_prompt_order_tie_uses_source_index_without_priority_reordering() -> None:
    selection = await select(entry(content="high", priority=99), entry(content="low", priority=-99))
    built = await PromptBuilder().build(
        PromptInput(lore=tuple(reversed(selection.entries))), budgets()
    )
    assert built.messages[0].content == "## キャラクターLore\nhigh\n\n## キャラクターLore\nlow"


@pytest.mark.parametrize("invalid", [True, -1, 1.5])
async def test_invalid_counter_result_is_safe(invalid: Any) -> None:
    def counter(messages: Any, *, tools: Any = ()) -> Any:
        return invalid

    with pytest.raises(ValueError, match="Invalid prompt token count"):
        await select_lore(book(entry()), "key", (), counter)
