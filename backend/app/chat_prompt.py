from collections.abc import Iterator
from dataclasses import dataclass, field

from app import chat_service
from app.characters.lore_selector import CharacterLoreSelection, select_character_lore
from app.characters.models import CharacterBook
from app.conversation_history.prompt_history import RestoredHistoryTurn
from app.conversation_history.service import HistorySession
from app.prompting import (
    BuiltPrompt,
    CharacterPrompt,
    CurrentUserMessage,
    HistoryCandidates,
    MaskedHistoryTurn,
    PromptBuildInput,
    PromptBuilder,
    PromptCharacterLoreTokenCounter,
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
    character_book: CharacterBook | None = None,
    rag: RagContext,
    current_user: CurrentUserMessage,
    history_session: HistorySession,
    config: ModelSettings,
    token_counter: TokenCounter,
) -> BuiltPrompt:
    try:
        prompt_input = _build_prompt_input(
            character=character,
            character_book=character_book,
            rag=rag,
            current_user=current_user,
            history_session=history_session,
            config=config,
            token_counter=token_counter,
        )
        return PromptBuilder(token_counter).build(prompt_input)
    except PromptInputLimitError as exc:
        raise _input_limit_error(exc, config) from exc


def _build_prompt_input(
    *,
    character: CharacterPrompt,
    character_book: CharacterBook | None = None,
    rag: RagContext,
    current_user: CurrentUserMessage,
    history_session: HistorySession,
    config: ModelSettings,
    token_counter: TokenCounter | None = None,
) -> PromptBuildInput:
    history_source = _ReplayableHistorySource(
        history_session=history_session,
        max_completed_turns=config.max_completed_turns,
    )
    history = HistoryCandidates(
        newest_first_factory=history_source.masked_turns_newest_first,
        omitted_turns=0,
    )
    character_lore = _select_character_lore(
        character_book,
        current_user,
        history_source,
        token_counter,
    )
    input_limit = (
        config.ollama_context_tokens - config.assistant_max_generation_tokens
    )
    return PromptBuildInput(
        character=character,
        character_lore=character_lore,
        rag=rag,
        history=history,
        current_user=current_user,
        budget=TokenBudget(
            total=input_limit,
            character=input_limit,
            character_lore=input_limit,
            rag=input_limit,
            history=config.history_token_limit,
            current_user=config.user_input_token_limit,
            post_history=input_limit,
        ),
    )


@dataclass
class _ReplayableHistorySource:
    history_session: HistorySession
    max_completed_turns: int
    _first_iterator: Iterator[RestoredHistoryTurn] | None = None
    _cached_prefix: list[RestoredHistoryTurn] = field(default_factory=list)
    _first_iterator_claimed: bool = False

    def scan_messages_newest_first(self, limit: int) -> tuple[str, ...]:
        if limit <= 0:
            return ()
        iterator = self._ensure_first_iterator()
        messages: list[str] = []
        while len(messages) < limit:
            try:
                turn = next(iterator)
            except StopIteration:
                break
            self._cached_prefix.append(turn)
            if turn.assistant_content is not None:
                messages.append(turn.assistant_content)
                if len(messages) == limit:
                    break
            messages.append(turn.user_content)
        return tuple(messages[:limit])

    def masked_turns_newest_first(self) -> Iterator[MaskedHistoryTurn]:
        if self._first_iterator is None or self._first_iterator_claimed:
            source = self._new_iterator()
            return (_masked_turn(turn) for turn in source)
        self._first_iterator_claimed = True
        cached_prefix = tuple(self._cached_prefix)
        first_iterator = self._first_iterator

        def replay_then_continue() -> Iterator[MaskedHistoryTurn]:
            for turn in cached_prefix:
                yield _masked_turn(turn)
            for turn in first_iterator:
                yield _masked_turn(turn)

        return replay_then_continue()

    def _ensure_first_iterator(self) -> Iterator[RestoredHistoryTurn]:
        if self._first_iterator is None:
            self._first_iterator = self._new_iterator()
        return self._first_iterator

    def _new_iterator(self) -> Iterator[RestoredHistoryTurn]:
        return self.history_session.prompt_turns(
            max_completed_turns=self.max_completed_turns,
            page_size=_PROMPT_HISTORY_PAGE_SIZE,
        )


def _masked_turn(turn: RestoredHistoryTurn) -> MaskedHistoryTurn:
    return MaskedHistoryTurn(
        turn.user_content,
        turn.assistant_content,
        turn.is_completed,
    )


def _select_character_lore(
    character_book: CharacterBook | None,
    current_user: CurrentUserMessage,
    history_source: _ReplayableHistorySource,
    token_counter: TokenCounter | None,
) -> CharacterLoreSelection:
    if character_book is None:
        return CharacterLoreSelection((), (), None, False)
    if token_counter is None:
        raise TypeError("token_counter is required with character_book")
    scan_depth = 1 if character_book.scan_depth is None else character_book.scan_depth
    history_limit = max(scan_depth - 1, 0)
    scan_messages = (
        current_user.content,
        *history_source.scan_messages_newest_first(history_limit),
    )
    return select_character_lore(
        character_book,
        scan_messages,
        PromptCharacterLoreTokenCounter(token_counter),
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
