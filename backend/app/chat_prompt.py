from app import chat_service
from app.conversation_history.service import HistorySession
from app.prompting import (
    BuiltPrompt,
    CharacterPrompt,
    CurrentUserMessage,
    HistoryCandidates,
    MaskedHistoryTurn,
    PromptBuildInput,
    PromptBuilder,
    PromptInputLimitError,
    RagContext,
    TokenBudget,
    TokenCounter,
)
from app.model_settings import ModelSettings

_PROMPT_HISTORY_PAGE_SIZE = 32


def build_chat_prompt(
    *,
    character: CharacterPrompt,
    rag: RagContext,
    current_user: CurrentUserMessage,
    history_session: HistorySession,
    config: ModelSettings,
    token_counter: TokenCounter,
) -> BuiltPrompt:
    try:
        prompt_input = _build_prompt_input(
            character=character,
            rag=rag,
            current_user=current_user,
            history_session=history_session,
            config=config,
        )
        return PromptBuilder(token_counter).build(prompt_input)
    except PromptInputLimitError as exc:
        raise _input_limit_error(exc, config) from exc


def _build_prompt_input(
    *,
    character: CharacterPrompt,
    rag: RagContext,
    current_user: CurrentUserMessage,
    history_session: HistorySession,
    config: ModelSettings,
) -> PromptBuildInput:
    history = HistoryCandidates(
        newest_first_factory=lambda: (
            MaskedHistoryTurn(
                turn.user_content,
                turn.assistant_content,
                turn.is_completed,
            )
            for turn in history_session.prompt_turns(
                max_completed_turns=config.max_completed_turns,
                page_size=_PROMPT_HISTORY_PAGE_SIZE,
            )
        ),
        omitted_turns=0,
    )
    input_limit = (
        config.ollama_context_tokens - config.assistant_max_generation_tokens
    )
    return PromptBuildInput(
        character=character,
        rag=rag,
        history=history,
        current_user=current_user,
        budget=TokenBudget(
            total=input_limit,
            character=input_limit,
            rag=input_limit,
            history=config.history_token_limit,
            current_user=config.user_input_token_limit,
            post_history=input_limit,
        ),
    )


def _input_limit_error(
    error: PromptInputLimitError,
    config: ModelSettings,
) -> chat_service.ChatInputLimitError:
    used = error.used
    limit = error.limit
    if error.region == "total":
        used += config.assistant_max_generation_tokens
        limit = config.ollama_context_tokens
    return chat_service.ChatInputLimitError(
        region=error.region,
        used=used,
        limit=limit,
    )
