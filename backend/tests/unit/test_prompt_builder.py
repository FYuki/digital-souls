import pytest

from app.prompting import (
    CharacterPrompt,
    CurrentUserMessage,
    HistoryCandidates,
    MaskedHistory,
    MaskedHistoryTurn,
    PromptBuildInput,
    PromptRole,
    RagContext,
)
from tests.prompt_test_support import (
    prompt_build_input,
    prompt_builder,
    token_budget,
)


class TestPromptBuilderComposition:
    def test_should_compose_all_regions_in_the_required_order(self) -> None:
        prompt_input = prompt_build_input()

        result = prompt_builder().build(prompt_input)

        assert [message.role for message in result.messages] == [
            PromptRole.SYSTEM,
            PromptRole.SYSTEM,
            PromptRole.USER,
            PromptRole.ASSISTANT,
            PromptRole.SYSTEM,
            PromptRole.USER,
        ]
        contents = [message.content for message in result.messages]
        assert contents[0].index("概要") < contents[0].index("性格")
        assert contents[0].index("性格") < contents[0].index("関係")
        assert contents[0].index("関係") < contents[0].index("システム指示")
        assert contents[0].index("システム指示") < contents[0].index("会話例")
        assert "RAG本文" in contents[1]
        assert contents[2:] == [
            "過去user",
            "過去assistant",
            "最終指示",
            "現在user原文",
        ]

    @pytest.mark.parametrize(
        ("field", "marker"),
        [
            ("description", "概要だけ"),
            ("personality", "性格だけ"),
            ("scenario", "関係だけ"),
            ("system_prompt", "指示だけ"),
            ("mes_example", "会話例だけ"),
        ],
    )
    def test_should_include_each_nonempty_character_field(
        self,
        field: str,
        marker: str,
    ) -> None:
        values = {
            "description": "",
            "personality": "",
            "scenario": "",
            "system_prompt": "",
            "mes_example": "",
        }
        values[field] = marker
        character = CharacterPrompt(
            **values,
            post_history_instructions="",
        )
        prompt_input = prompt_build_input(
            character=character,
            rag=RagContext(items=()),
            history=MaskedHistory(turns=(), omitted_turns=0),
        )

        result = prompt_builder().build(prompt_input)

        assert len(result.messages) == 2
        assert marker in result.messages[0].content
        assert result.messages[1].content == "現在user原文"

    def test_should_omit_empty_character_sections_and_empty_optional_regions(
        self,
    ) -> None:
        character = CharacterPrompt(
            description="  ",
            personality="",
            scenario="",
            system_prompt="守る指示",
            mes_example="",
            post_history_instructions="",
        )
        prompt_input = prompt_build_input(
            character=character,
            rag=RagContext(items=()),
            history=MaskedHistory(turns=(), omitted_turns=0),
        )

        result = prompt_builder().build(prompt_input)

        assert [message.role for message in result.messages] == [
            PromptRole.SYSTEM,
            PromptRole.USER,
        ]
        system_lines = result.messages[0].content.splitlines()
        assert system_lines[-1] == "守る指示"
        assert sum(line.startswith("## ") for line in system_lines) == 1

    def test_should_keep_current_user_separate_when_body_matches_history(self) -> None:
        repeated_body = "同じ本文"
        history = MaskedHistory(
            turns=(
                MaskedHistoryTurn(
                    user_content=repeated_body,
                    assistant_content="過去の回答",
                    is_completed=True,
                ),
            ),
            omitted_turns=0,
        )
        prompt_input = prompt_build_input(
            history=history,
            current_user=CurrentUserMessage(content=repeated_body),
        )

        result = prompt_builder().build(prompt_input)

        repeated_messages = [
            message
            for message in result.messages
            if message.content == repeated_body
        ]
        assert [message.role for message in repeated_messages] == [
            PromptRole.USER,
            PromptRole.USER,
        ]
        assert result.messages[-1].content == repeated_body

    def test_should_reject_masked_history_in_the_current_user_boundary(self) -> None:
        history = MaskedHistory(
            turns=(
                MaskedHistoryTurn(
                    user_content="マスク済み",
                    assistant_content="回答",
                    is_completed=True,
                ),
            ),
            omitted_turns=0,
        )

        with pytest.raises(TypeError, match="CurrentUserMessage"):
            PromptBuildInput(
                character=CharacterPrompt(
                    description="概要",
                    personality="",
                    scenario="",
                    system_prompt="指示",
                    mes_example="",
                    post_history_instructions="",
                ),
                rag=RagContext(items=()),
                history=HistoryCandidates(
                    newest_first_factory=lambda: iter(()), omitted_turns=0
                ),
                current_user=history,  # type: ignore[arg-type]
                budget=token_budget(),
            )
