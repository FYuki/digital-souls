import httpx

from app import chat_service
from app.conversation_history.service import HistorySession
from app.llm import router as _llm_router
from app.prompting import (
    BuiltPrompt,
    CharacterPrompt,
    CurrentUserMessage,
    HistoryCandidates,
    MaskedHistoryTurn,
    PromptBuildInput,
    PromptBuilder,
    PromptInputLimitError,
    PromptMessage,
    RagContext,
    TokenBudget,
)
from app.prompting.config import PromptRuntimeConfig

_PROMPT_HISTORY_PAGE_SIZE = 32


class _ChatTokenCounter:
    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        try:
            return _llm_router.count_input_tokens(messages)
        except httpx.TimeoutException as exc:
            raise chat_service.ChatTimeoutError() from exc
        except httpx.HTTPError as exc:
            raise chat_service.ChatBackendError() from exc


def build_chat_prompt(
    *,
    character: CharacterPrompt,
    rag: RagContext,
    current_user: CurrentUserMessage,
    history_session: HistorySession,
    config: PromptRuntimeConfig,
) -> BuiltPrompt:
    token_counter = _ChatTokenCounter()
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
    config: PromptRuntimeConfig,
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
    input_limit = config.context_token_limit - config.assistant_max_generation_tokens
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
    config: PromptRuntimeConfig,
) -> chat_service.ChatInputLimitError:
    used = error.used
    limit = error.limit
    if error.region == "total":
        used += config.assistant_max_generation_tokens
        limit = config.context_token_limit
    return chat_service.ChatInputLimitError(
        region=error.region,
        used=used,
        limit=limit,
    )
