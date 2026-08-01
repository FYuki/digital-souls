from app.prompting import (
    CharacterPrompt,
    CurrentUserMessage,
    HistoryCandidates,
    MaskedHistory,
    MaskedHistoryTurn,
    PromptMessage,
    PromptBuildInput,
    PromptBuilder,
    RagContext,
    RagItem,
    TokenBudget,
)


class UnitTokenCounter:
    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        return sum(message.content != "" for message in messages)


def token_budget(
    *,
    total: int = 20,
    character: int = 10,
    rag: int = 10,
    history: int = 10,
    current_user: int = 10,
    post_history: int = 10,
) -> TokenBudget:
    return TokenBudget(
        total=total,
        character=character,
        rag=rag,
        history=history,
        current_user=current_user,
        post_history=post_history,
    )


def prompt_build_input(
    *,
    character: CharacterPrompt | None = None,
    rag: RagContext | None = None,
    history: MaskedHistory | None = None,
    current_user: CurrentUserMessage | None = None,
    budget: TokenBudget | None = None,
) -> PromptBuildInput:
    masked_history = (
        history
        if history is not None
        else MaskedHistory(
            turns=(
                MaskedHistoryTurn(
                    user_content="過去user",
                    assistant_content="過去assistant",
                    is_completed=True,
                ),
            ),
            omitted_turns=0,
        )
    )
    return PromptBuildInput(
        character=character
        if character is not None
        else CharacterPrompt(
            description="概要",
            personality="性格",
            scenario="関係",
            system_prompt="システム指示",
            mes_example="会話例",
            post_history_instructions="最終指示",
        ),
        rag=rag if rag is not None else RagContext(items=(RagItem("RAG本文"),)),
        history=HistoryCandidates(
            newest_first_factory=lambda: reversed(masked_history.turns),
            omitted_turns=masked_history.omitted_turns,
        ),
        current_user=current_user
        if current_user is not None
        else CurrentUserMessage(content="現在user原文"),
        budget=budget if budget is not None else token_budget(),
    )


def prompt_builder() -> PromptBuilder:
    return PromptBuilder(token_counter=UnitTokenCounter())
