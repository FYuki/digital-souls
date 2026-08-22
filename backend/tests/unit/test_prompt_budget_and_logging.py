import logging
from datetime import UTC, datetime

import pytest

from app.prompting import (
    CharacterPrompt,
    CurrentUserMessage,
    MaskedHistory,
    MaskedHistoryTurn,
    PromptBuildInput,
    PromptInputLimitError,
    RagContext,
    RagItem,
    PromptMemoryReference,
    TokenBudget,
)
from tests.prompt_test_support import (
    prompt_build_input,
    prompt_builder,
    token_budget,
)

PROMPT_BODY_SECRETS = (
    "SECRET_DESCRIPTION_51",
    "SECRET_PERSONALITY_52",
    "SECRET_SCENARIO_53",
    "SECRET_SYSTEM_54",
    "SECRET_EXAMPLE_55",
    "SECRET_POST_56",
    "SECRET_RAG_57",
    "SECRET_HISTORY_USER_58",
    "SECRET_HISTORY_ASSISTANT_59",
    "SECRET_CURRENT_60",
)


def _prompt_input_with_secret_bodies(budget: TokenBudget) -> PromptBuildInput:
    (
        description,
        personality,
        scenario,
        system_prompt,
        mes_example,
        post_history,
        rag,
        history_user,
        history_assistant,
        current_user,
    ) = PROMPT_BODY_SECRETS
    return prompt_build_input(
        character=CharacterPrompt(
            description=description,
            personality=personality,
            scenario=scenario,
            system_prompt=system_prompt,
            mes_example=mes_example,
            post_history_instructions=post_history,
        ),
        rag=RagContext(items=(RagItem(rag, raw_distance=1.25),)),
        history=MaskedHistory(
            turns=(MaskedHistoryTurn(history_user, history_assistant, True),),
            omitted_turns=0,
        ),
        current_user=CurrentUserMessage(current_user),
        budget=budget,
    )


class TestPromptBuilderBudget:
    def test_should_report_usage_for_each_budget_region(self) -> None:
        result = prompt_builder().build(prompt_build_input())

        assert result.usage.character == 1
        assert result.usage.rag == 1
        assert result.usage.history == 2
        assert result.usage.current_user == 1
        assert result.usage.post_history == 1
        assert result.usage.total == 6

    def test_should_drop_rag_before_old_history_at_total_limit(self) -> None:
        history = MaskedHistory(
            turns=(
                MaskedHistoryTurn("古いuser", "古いassistant", True),
                MaskedHistoryTurn("直前user", "直前assistant", True),
            ),
            omitted_turns=0,
        )
        prompt_input = prompt_build_input(
            history=history,
            budget=token_budget(total=7),
        )

        result = prompt_builder().build(prompt_input)

        contents = [message.content for message in result.messages]
        assert all("RAG本文" not in content for content in contents)
        assert "古いuser" in contents
        assert "直前user" in contents
        assert result.usage.omitted_rag_items == 1
        assert result.usage.omitted_history_exchanges == 0

    def test_should_keep_ranked_rag_prefix_at_region_limit(self) -> None:
        rag = RagContext(
            items=(
                RagItem("順位1", raw_distance=0.01),
                RagItem("順位2", raw_distance=0.02),
                RagItem("順位3", raw_distance=0.03),
            )
        )

        result = prompt_builder().build(
            prompt_build_input(rag=rag, budget=token_budget(rag=2))
        )

        selected_items = [
            message.content.rsplit("\n", maxsplit=1)[-1]
            for message in result.messages
            if message.content.endswith(("順位1", "順位2", "順位3"))
        ]
        assert selected_items == ["順位1", "順位2"]
        assert result.usage.omitted_rag_items == 1

    def test_should_propagate_references_only_for_rag_items_selected_by_budget(
        self,
    ) -> None:
        first = PromptMemoryReference(
            memory_id="00000000-0000-4000-8000-000000000043",
            occurred_at=datetime(2025, 3, 1, tzinfo=UTC),
            occurred_precision="DAY",
            match_kind="BOTH",
        )
        omitted = PromptMemoryReference(
            memory_id="00000000-0000-4000-8000-000000000044",
            occurred_at=None,
            occurred_precision=None,
            match_kind="SEMANTIC",
        )
        rag = RagContext(
            items=(
                RagItem("順位1", raw_distance=0.01, reference=first),
                RagItem("順位2", raw_distance=0.02, reference=omitted),
            )
        )

        result = prompt_builder().build(
            prompt_build_input(rag=rag, budget=token_budget(rag=1))
        )

        assert tuple(
            message.memory_reference
            for message in result.messages
            if message.memory_reference is not None
        ) == (first,)
        assert result.usage.omitted_rag_items == 1

    def test_should_keep_ranked_rag_prefix_when_total_limit_reduces_rag(
        self,
    ) -> None:
        rag = RagContext(
            items=(
                RagItem("順位1", raw_distance=0.01),
                RagItem("順位2", raw_distance=0.02),
                RagItem("順位3", raw_distance=0.03),
            )
        )

        result = prompt_builder().build(
            prompt_build_input(rag=rag, budget=token_budget(total=7))
        )

        selected_items = [
            message.content.rsplit("\n", maxsplit=1)[-1]
            for message in result.messages
            if message.content.endswith(("順位1", "順位2", "順位3"))
        ]
        assert selected_items == ["順位1", "順位2"]
        assert result.usage.omitted_rag_items == 1

    def test_should_keep_required_rag_instruction_when_all_items_are_omitted(
        self,
    ) -> None:
        instruction = "期間内の記憶がないため推測で補完しない"
        rag = RagContext(
            items=(RagItem("削除対象", raw_distance=0.01),),
            required_instruction=instruction,
        )

        result = prompt_builder().build(
            prompt_build_input(rag=rag, budget=token_budget(rag=1, total=5))
        )

        contents = [message.content for message in result.messages]
        assert instruction in contents
        assert all("削除対象" not in content for content in contents)
        assert result.usage.omitted_rag_items == 1

    def test_should_drop_oldest_history_while_preserving_latest_exchange(
        self,
    ) -> None:
        history = MaskedHistory(
            turns=(
                MaskedHistoryTurn("最古user", "最古assistant", True),
                MaskedHistoryTurn("直前user", "直前assistant", True),
            ),
            omitted_turns=0,
        )
        prompt_input = prompt_build_input(
            rag=RagContext(items=()),
            history=history,
            budget=token_budget(history=2),
        )

        result = prompt_builder().build(prompt_input)

        contents = [message.content for message in result.messages]
        assert "最古user" not in contents
        assert "最古assistant" not in contents
        assert "直前user" in contents
        assert "直前assistant" in contents
        assert result.usage.omitted_history_exchanges == 1

    @pytest.mark.parametrize(
        ("budget", "omitted_content"),
        [
            (token_budget(rag=0), "RAG本文"),
            (token_budget(post_history=0), "最終指示"),
        ],
    )
    def test_should_apply_optional_region_budgets_independently(
        self,
        budget: TokenBudget,
        omitted_content: str,
    ) -> None:
        result = prompt_builder().build(prompt_build_input(budget=budget))

        contents = [message.content for message in result.messages]
        assert all(omitted_content not in content for content in contents)
        assert "現在user原文" in contents

    @pytest.mark.parametrize(
        ("budget", "region"),
        [
            (token_budget(character=0), "character"),
            (token_budget(current_user=0), "current_user"),
            (token_budget(total=3), "total"),
        ],
    )
    def test_should_raise_typed_limit_error_when_required_content_exceeds_budget(
        self,
        budget: TokenBudget,
        region: str,
    ) -> None:
        prompt_input = prompt_build_input(
            rag=RagContext(items=()),
            budget=budget,
        )

        with pytest.raises(PromptInputLimitError) as captured:
            prompt_builder().build(prompt_input)

        assert captured.value.region == region
        assert captured.value.used > captured.value.limit

    def test_should_drop_post_history_after_rag_and_old_history_at_total_limit(
        self,
    ) -> None:
        history = MaskedHistory(
            turns=(
                MaskedHistoryTurn("古いuser", "古いassistant", False),
                MaskedHistoryTurn("直前user", "直前assistant", True),
            ),
            omitted_turns=0,
        )
        prompt_input = prompt_build_input(
            history=history,
            budget=token_budget(total=4),
        )

        result = prompt_builder().build(prompt_input)

        contents = [message.content for message in result.messages]
        assert contents[0].startswith("## キャラクター概要\n")
        assert contents[1:] == [
            "直前user",
            "直前assistant",
            "現在user原文",
        ]
        assert "RAG本文" not in contents
        assert "古いuser" not in contents
        assert "最終指示" not in contents

    def test_should_reject_total_limit_that_cannot_keep_required_regions(
        self,
    ) -> None:
        prompt_input = prompt_build_input(
            rag=RagContext(items=()),
            history=MaskedHistory(turns=(), omitted_turns=0),
            budget=token_budget(total=1),
        )

        with pytest.raises(PromptInputLimitError) as captured:
            prompt_builder().build(prompt_input)

        assert captured.value.region == "total"
        assert captured.value.used == 2
        assert captured.value.limit == 1


class TestPromptBuilderLogging:
    def test_should_not_expose_prompt_bodies_in_input_or_output_repr(
        self,
    ) -> None:
        prompt_input = _prompt_input_with_secret_bodies(token_budget())

        result = prompt_builder().build(prompt_input)

        prompt_values = (
            prompt_input.character,
            prompt_input.rag.items[0],
            prompt_input.rag,
            prompt_input.history,
            prompt_input.current_user,
            prompt_input,
            *result.messages,
            result,
        )
        assert all(
            secret not in repr(value)
            for value in prompt_values
            for secret in PROMPT_BODY_SECRETS
        )

    def test_should_not_log_any_prompt_body_on_success(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        prompt_input = _prompt_input_with_secret_bodies(token_budget())
        caplog.set_level(logging.DEBUG)

        prompt_builder().build(prompt_input)

        assert all(secret not in caplog.text for secret in PROMPT_BODY_SECRETS)

    def test_should_not_leak_any_prompt_body_in_total_limit_error_or_logs(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        prompt_input = _prompt_input_with_secret_bodies(
            token_budget(total=0),
        )
        caplog.set_level(logging.DEBUG)

        with pytest.raises(PromptInputLimitError) as captured:
            prompt_builder().build(prompt_input)

        error_text = str(captured.value)
        error_repr = repr(captured.value)
        assert captured.value.region == "total"
        assert all(
            secret not in error_text
            and secret not in error_repr
            and secret not in caplog.text
            for secret in PROMPT_BODY_SECRETS
        )
