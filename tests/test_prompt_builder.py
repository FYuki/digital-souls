"""Pure ADR 0026 contracts, using synthetic text and deterministic token costs."""

import json
from collections.abc import Sequence
from dataclasses import replace

import pytest

from digital_souls_core.contracts import FunctionCall, FunctionDefinition, Message, Tool, ToolCall
from digital_souls_core.prompt_builder import (
    MEMORY_NOTICE,
    ByteTokenCounter,
    CharacterCard,
    HistoryTurn,
    MemoryItem,
    PromptBudgetExceeded,
    PromptBuilder,
    PromptInput,
    PromptResult,
    Region,
    RuntimeContext,
    SelectedLore,
    TokenBudgets,
    convert_messages,
    split_messages,
)

pytestmark = pytest.mark.ut

REGIONS = (
    "character",
    "lore",
    "memory",
    "history",
    "current_input",
    "post_history",
)


def budgets(total: int = 10000, **limits: int) -> TokenBudgets:
    return TokenBudgets(total=total, **{region: limits.get(region, 10000) for region in REGIONS})


def msg(role: str, text: str) -> Message:
    return Message.model_validate({"role": role, "content": text})


def tool_pair(identifier: str = "call-one") -> tuple[Message, Message]:
    return (
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id=identifier,
                    type="function",
                    function=FunctionCall(name="lookup", arguments='{"q":"茶"}'),
                )
            ],
        ),
        Message(role="tool", content="result", tool_call_id=identifier),
    )


def turn(index: int) -> HistoryTurn:
    return HistoryTurn((msg("user", f"u{index}"), msg("assistant", f"a{index}")))


def sample() -> PromptInput:
    return PromptInput(
        card=CharacterCard(description="persona", post_history_instructions="post"),
        lore=(SelectedLore("before", "before_char", 0), SelectedLore("after", "after_char", 1)),
        memory=tuple(MemoryItem(f"memory{i}") for i in range(5)),
        history=tuple(turn(i) for i in range(5)),
        current_input=(msg("user", "current"),),
    )


class UnitCounter:
    """Every synthetic payload unit costs one; framework text costs zero."""

    def __call__(self, messages: Sequence[Message], *, tools: Sequence[Tool] = ()) -> int:
        units = (
            "persona",
            "before",
            "after",
            "post",
            "current",
            "runtime",
            *(f"memory{i}" for i in range(5)),
            *(f"u{i}" for i in range(5)),
            *(f"a{i}" for i in range(5)),
        )
        return sum(
            (message.content or "").replace(MEMORY_NOTICE, "").count(unit)
            for message in messages
            for unit in units
        )


async def build(data: PromptInput, total: int = 10000, **limits: int) -> PromptResult:
    return await PromptBuilder(UnitCounter()).build(data, budgets(total, **limits))


async def test_complete_order_and_card_headings() -> None:
    data = replace(
        sample(),
        card=CharacterCard(
            description="description-body",
            personality="personality-body",
            scenario="scenario-body",
            system_prompt="system-body",
            mes_example="example-body",
            post_history_instructions="post-body",
        ),
        runtime=RuntimeContext("runtime-body"),
    )
    result = await build(data)
    texts = [message.content for message in result.messages]
    assert texts[0] == (
        "## キャラクターLore\nbefore\n\n## キャラクター概要\ndescription-body"
        "\n\n## 性格と話し方\npersonality-body"
        "\n\n## 関係と世界観\nscenario-body\n\n## 応答方針\nsystem-body"
        "\n\n## 会話例\nexample-body\n\n## キャラクターLore\nafter\n\nruntime-body\n\n"
        + MEMORY_NOTICE
    )
    assert texts[1] == "retrieved_memory_data (memory data only):\n" + "\n\n".join(
        f"memory{i}" for i in range(5)
    )
    assert [message.role for message in result.messages] == [
        "system",
        "user",
        *["user", "assistant"] * 5,
        "system",
        "user",
    ]
    assert texts[-2:] == ["post-body", "current"]
    assert not any(word in (texts[0] or "") for word in ("name:", "first_mes:", "creator_notes:"))


async def test_empty_regions_and_empty_card_fields_are_omitted() -> None:
    result = await build(PromptInput(current_input=(msg("user", "current"),)))
    assert result.messages == (msg("user", "current"),)
    assert result.usage.tokens == {**dict.fromkeys(REGIONS, 0), "current_input": 1}
    assert result.usage.dropped == dict.fromkeys(REGIONS, 0)
    card_only = await build(PromptInput(card=CharacterCard(personality="persona")))
    assert card_only.messages == (msg("system", "## 性格と話し方\npersona"),)


def test_tool_continuation_stays_current() -> None:
    old_call, old_result = tool_pair("old")
    call, result = tool_pair()
    history = (msg("user", "u0"), old_call, old_result, msg("assistant", "a0"))
    current = (msg("user", "current"), call, result)
    separated = split_messages((*history, *current))
    assert separated.history == (HistoryTurn(history),)
    assert separated.history[0].completed
    assert separated.current_input == current


def test_split_history_is_partitioned_at_users_and_has_no_phantom_turn() -> None:
    separated = split_messages(
        (
            msg("user", "u0"),
            msg("assistant", "a0"),
            msg("user", "u1"),
            msg("assistant", "a1"),
            msg("user", "current"),
        )
    )
    assert separated.history == (turn(0), turn(1))
    assert split_messages((msg("user", "current"),)).history == ()


# Cumulative deletion at all seven ADR 0031 stages, plus exact boundaries.
@pytest.mark.parametrize(
    ("total", "memory", "history", "lore", "post"),
    [
        (20, 0, 0, 0, 0),
        (18, 2, 0, 0, 0),
        (16, 4, 0, 0, 0),
        (12, 4, 2, 0, 0),
        (11, 5, 2, 0, 0),
        (7, 5, 4, 0, 0),
        (5, 5, 4, 2, 0),
        (4, 5, 4, 2, 1),
    ],
)
async def test_all_seven_reduction_stages(
    total: int, memory: int, history: int, lore: int, post: int
) -> None:
    result = await build(sample(), total)
    assert result.usage.total_tokens == total
    assert result.usage.dropped == {
        "character": 0,
        "lore": lore,
        "memory": memory,
        "history": history,
        "current_input": 0,
        "post_history": post,
    }
    text = "\n".join(message.content or "" for message in result.messages)
    assert "persona" in text and "current" in text and "u4" in text and "a4" in text
    for index in range(5):
        assert (f"memory{index}" in text) == (index < 5 - memory)
        assert (f"u{index}" in text) == (index >= history)
    assert ("before" in text) == (lore == 0)
    assert ("after" in text) == (lore < 2)
    assert ("post" in text) == (post == 0)


@pytest.mark.parametrize(
    ("floor", "count", "total", "dropped_memory"),
    [(3, 3, 18, 0), (3, 2, 17, 0), (5, 5, 20, 0), (8, 5, 20, 0), (4, 5, 19, 1), (1, 5, 16, 4)],
)
async def test_configurable_memory_floor_and_boundary(
    floor: int, count: int, total: int, dropped_memory: int
) -> None:
    data = replace(sample(), memory=sample().memory[:count])
    result = await PromptBuilder(UnitCounter(), memory_floor=floor).build(data, budgets(total))
    assert result.usage.dropped["memory"] == dropped_memory
    assert result.usage.dropped["history"] == 0


@pytest.mark.parametrize(
    ("floor", "count", "total", "dropped_history", "dropped_memory"),
    [
        (3, 3, 11, 0, 5),
        (3, 2, 9, 0, 5),
        (5, 5, 15, 0, 5),
        (4, 5, 14, 1, 4),
        (1, 5, 8, 4, 4),
    ],
)
async def test_configurable_history_floor_includes_protected_turn(
    floor: int, count: int, total: int, dropped_history: int, dropped_memory: int
) -> None:
    data = replace(sample(), history=sample().history[-count:])
    result = await PromptBuilder(UnitCounter(), history_floor=floor).build(data, budgets(total))
    assert result.usage.dropped["history"] == dropped_history
    assert result.usage.dropped["memory"] == dropped_memory


@pytest.mark.parametrize(
    ("region", "data", "limit"),
    [
        ("character", PromptInput(card=CharacterCard(description="persona")), 0),
        ("character", PromptInput(runtime=RuntimeContext("runtime")), 0),
        ("current_input", PromptInput(current_input=(msg("user", "current"),)), 0),
        ("history", PromptInput(history=(turn(0),)), 1),
    ],
)
async def test_protected_region_overflow_is_private(
    region: str, data: PromptInput, limit: int
) -> None:
    with pytest.raises(PromptBudgetExceeded) as caught:
        await build(data, **{region: limit})
    assert caught.value.region == region
    assert caught.value.status == 400
    assert str(caught.value) == f"Prompt token budget exceeded: {region}"
    for text in ("persona", "runtime-body", "current-body", "u0", "a0"):
        assert text not in repr(caught.value)
        assert text not in str(caught.value)


async def test_protected_total_overflow_and_exact_boundary() -> None:
    data = PromptInput(
        card=CharacterCard(description="persona"),
        history=(turn(0),),
        current_input=(msg("user", "current"),),
    )
    assert (await build(data, 4)).usage.total_tokens == 4
    with pytest.raises(PromptBudgetExceeded) as caught:
        await build(sample(), 3 - 1)
    assert caught.value.region == "total"
    assert not any(text in repr(caught.value) for text in ("persona", "current-body", "memory0"))


@pytest.mark.parametrize(
    ("region", "limit", "dropped"),
    [
        ("memory", 2, 3),
        ("history", 4, 3),
        ("lore", 1, 1),
        ("post_history", 0, 1),
    ],
)
async def test_optional_region_limits_remove_only_the_over_budget_region(
    region: Region, limit: int, dropped: int
) -> None:
    result = await build(sample(), **{str(region): limit})
    assert result.usage.tokens[region] <= limit
    assert result.usage.dropped == {**dict.fromkeys(REGIONS, 0), region: dropped}


async def test_old_tool_turn_is_dropped_whole_and_latest_complete_tool_turn_is_protected() -> None:
    call, result = tool_pair()
    complete = HistoryTurn((msg("user", "u1"), call, result, msg("assistant", "a1")))
    data = PromptInput(history=(turn(0), complete), current_input=(msg("user", "current"),))
    built = await build(data, history=2)
    assert built.messages == (*complete.messages, *data.current_input)
    assert built.usage.dropped["history"] == 1
    with pytest.raises(PromptBudgetExceeded, match="history"):
        await build(data, history=1)


async def test_incomplete_history_does_not_displace_last_completed_turn() -> None:
    call, result = tool_pair()
    unfinished = HistoryTurn((msg("user", "u1"), call, result))
    assert not unfinished.completed
    data = PromptInput(history=(turn(0), unfinished), current_input=(msg("user", "current"),))
    built = await build(data, history=2)
    assert built.messages == (*turn(0).messages, *data.current_input)
    assert built.usage.dropped["history"] == 1


async def test_without_completed_history_all_history_is_optional() -> None:
    data = PromptInput(
        history=(HistoryTurn((msg("user", "u0"),)),), current_input=(msg("user", "current"),)
    )
    assert (await build(data, history=0)).messages == data.current_input


async def test_usage_and_all_prompt_container_reprs_hide_bodies_and_inputs_are_unchanged() -> None:
    data = sample()
    original = convert_messages(data)
    result = await build(data, 9)
    assert convert_messages(data) == original
    assert result.usage.tokens == {
        "character": 1,
        "lore": 2,
        "memory": 0,
        "history": 4,
        "current_input": 1,
        "post_history": 1,
    }
    assert result.usage.total_tokens == 9
    for value in (
        data,
        data.card,
        data.lore,
        data.runtime,
        data.memory,
        data.history,
        result,
        result.usage,
        split_messages((msg("user", "current"),)),
    ):
        assert not any(body in repr(value) for body in ("persona", "memory0", "current-body", "u0"))
    result.messages[-1].content = "changed"
    assert data.current_input[-1].content == "current"


async def test_converter_can_be_replaced_and_budget_counts_the_final_conversion() -> None:
    def alternate(data: PromptInput) -> tuple[Message, ...]:
        return (*convert_messages(data), msg("system", "runtime"))

    result = await PromptBuilder(UnitCounter(), converter=alternate).build(
        PromptInput(current_input=(msg("user", "current"),)), budgets(2)
    )
    assert result.messages[-1].content == "runtime"
    assert result.usage.total_tokens == 2
    with pytest.raises(PromptBudgetExceeded, match="total"):
        await PromptBuilder(UnitCounter(), converter=alternate).build(
            PromptInput(current_input=(msg("user", "current"),)), budgets(1)
        )


def test_byte_estimate_counts_utf8_call_names_arguments_tool_definitions_and_overhead() -> None:
    call, result = tool_pair()
    messages = (msg("system", "日本語"), msg("user", "hello"), call, result)
    tools = (
        Tool(
            type="function",
            function=FunctionDefinition(
                name="lookup", description="説明", parameters={"type": "object"}
            ),
        ),
    )
    definition = json.dumps(
        tools[0].model_dump(exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    body = "日本語hellolookup" + '{"q":"茶"}' + "result" + definition
    counter = ByteTokenCounter()
    assert counter(messages, tools=tools) == len(body.encode("utf-8")) + len(messages) * 16 + 16
    assert counter(()) == 16
    assert counter((msg("user", ""),)) == 32
    assert counter(messages) == len(body.removesuffix(definition).encode("utf-8")) + 80


async def test_tools_are_counted_once_globally_and_protected_as_current_input() -> None:
    tools = (Tool(type="function", function=FunctionDefinition(name="lookup", parameters={})),)
    data = PromptInput(
        card=CharacterCard(description="persona"),
        tools=tools,
        current_input=(msg("user", "current"),),
    )
    counter = ByteTokenCounter()
    builder = PromptBuilder(counter)
    result = await builder.build(data, budgets())
    assert result.usage.total_tokens == counter(result.messages, tools=tools)
    assert result.usage.tokens["current_input"] == counter(data.current_input, tools=tools)
    with pytest.raises(PromptBudgetExceeded, match="current_input"):
        await builder.build(data, budgets(current_input=counter(data.current_input)))


@pytest.mark.parametrize(
    "messages",
    [
        (),
        (msg("user", "current"), tool_pair()[0]),
    ],
)
def test_invalid_partition_rejects_without_echoing_body(
    messages: tuple[Message, ...],
) -> None:
    with pytest.raises(ValueError) as caught:
        split_messages(messages)
    assert "synthetic-body" not in repr(caught.value)


@pytest.mark.parametrize(("memory_floor", "history_floor"), [(0, 3), (-1, 3), (3, 0), (3, -1)])
def test_invalid_floors_are_rejected(memory_floor: int, history_floor: int) -> None:
    with pytest.raises(ValueError):
        PromptBuilder(UnitCounter(), memory_floor=memory_floor, history_floor=history_floor)


async def test_lore_order_is_stable_by_insertion_order_across_positions() -> None:
    data = PromptInput(
        card=CharacterCard(description="persona"),
        lore=(SelectedLore("after", "after_char", 0), SelectedLore("before", "before_char", 1)),
    )
    result = await build(data, lore=1)
    assert (
        result.messages[0].content == "## キャラクターLore\nbefore\n\n## キャラクター概要\npersona"
    )
    assert result.usage.dropped["lore"] == 1


async def test_only_last_occurrence_of_reused_history_turn_is_protected() -> None:
    repeated = turn(0)
    data = PromptInput(history=(repeated, repeated), current_input=(msg("user", "current"),))
    result = await build(data, history=2)
    assert result.messages == (*repeated.messages, *data.current_input)
    assert result.usage.dropped["history"] == 1


@pytest.mark.parametrize("region", ["character", "history", "current_input", "total"])
async def test_error_and_reprs_never_echo_any_prompt_body(region: str) -> None:
    body = "synthetic-private-body"
    data = PromptInput(
        card=CharacterCard(description=body, post_history_instructions=body),
        lore=(SelectedLore(body),),
        runtime=RuntimeContext(body),
        memory=(MemoryItem(body),),
        history=(HistoryTurn((msg("user", body), msg("assistant", body))),),
        current_input=(msg("user", body),),
    )
    with pytest.raises(PromptBudgetExceeded) as caught:
        await PromptBuilder().build(data, budgets(**{region: 0}))
    assert caught.value.region == region
    assert body not in str(caught.value) and body not in repr(caught.value)
    assert body not in repr(vars(caught.value))
    for value in (data, data.card, data.lore, data.runtime, data.memory, data.history):
        assert body not in repr(value)
    result = await PromptBuilder().build(data, budgets())
    assert body not in repr(result) and body not in repr(result.usage)


async def test_builder_is_silent_even_when_optional_text_is_dropped(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    await build(sample(), 4)
    assert not caplog.records
    assert capsys.readouterr() == ("", "")


def test_lore_sorts_by_order_and_preserves_ties_before_pruning() -> None:
    data = PromptInput(
        lore=(
            SelectedLore("third", "after_char", 3),
            SelectedLore("first", "before_char", 1),
            SelectedLore("second", "before_char", 1),
        )
    )
    assert convert_messages(data) == (
        msg(
            "system",
            "## キャラクターLore\nfirst\n\n## キャラクターLore\nsecond"
            "\n\n## キャラクターLore\nthird",
        ),
    )


async def test_current_tool_continuation_fits_exact_budget_and_is_never_cut() -> None:
    data = PromptInput(current_input=(msg("user", "current"), *tool_pair()))
    counter = ByteTokenCounter()
    cost = counter(data.current_input)
    builder = PromptBuilder(counter)
    result = await builder.build(data, budgets(total=cost, current_input=cost))
    assert result.messages == data.current_input
    assert result.usage.tokens["current_input"] == cost
    assert result.usage.dropped["current_input"] == 0
    with pytest.raises(PromptBudgetExceeded, match="current_input"):
        await builder.build(data, budgets(current_input=cost - 1))


async def test_leading_assistant_is_a_deletable_incomplete_history_unit() -> None:
    prefix = (msg("assistant", "u1"), *tool_pair("prefix"))
    separated = split_messages(
        (*prefix, msg("user", "u0"), msg("assistant", "a0"), msg("user", "current"))
    )
    assert separated.history[0].messages == prefix
    assert not separated.history[0].completed
    result = await build(
        PromptInput(history=separated.history, current_input=separated.current_input), history=2
    )
    assert result.messages == (*turn(0).messages, msg("user", "current"))
    assert result.usage.dropped["history"] == 1


async def test_whitespace_card_fields_are_stripped_and_empty_heading_is_omitted() -> None:
    result = await build(
        PromptInput(card=CharacterCard(description="  persona  ", personality="  "))
    )
    assert result.messages == (msg("system", "## キャラクター概要\npersona"),)


@pytest.mark.parametrize(
    "messages",
    [
        (msg("assistant", "greeting"),),
        (msg("user", "current"), msg("assistant", "answer")),
    ],
)
def test_partition_preserves_other_http_accepted_sequences(
    messages: tuple[Message, ...],
) -> None:
    separated = split_messages(messages)
    rebuilt = convert_messages(
        PromptInput(
            history=separated.history,
            current_input=separated.current_input,
        )
    )
    assert [m for m in rebuilt if m.role != "system"] == [m for m in messages if m.role != "system"]


async def test_json_memory_items_remain_one_array_after_whole_item_reduction() -> None:
    data = PromptInput(
        memory=tuple(
            MemoryItem(json.dumps({"text": f"memory{i}"}), json_object=True) for i in range(5)
        ),
        current_input=(msg("user", "current"),),
    )
    result = await build(data, memory=3)
    assert json.loads((result.messages[1].content or "").split("\n", 1)[1]) == [
        {"text": f"memory{i}"} for i in range(3)
    ]
    assert result.usage.dropped["memory"] == 2


async def test_leading_assistant_does_not_count_toward_completed_history_floor() -> None:
    data = PromptInput(
        card=CharacterCard(description="persona"),
        memory=(MemoryItem("memory0"),),
        history=(HistoryTurn((msg("assistant", "u4"),)), turn(0), turn(1)),
        current_input=(msg("user", "current"),),
    )
    result = await build(data, total=7)
    assert result.usage.dropped["history"] == 1
    assert result.usage.dropped["memory"] == 0
    assert result.messages[-5:] == (*turn(0).messages, *turn(1).messages, msg("user", "current"))
