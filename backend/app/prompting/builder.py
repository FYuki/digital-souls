import logging
from dataclasses import dataclass
from typing import Protocol

from app.prompting.models import (
    BuiltPrompt,
    MaskedHistoryExchange,
    PromptBuildInput,
    PromptInputLimitError,
    PromptMessage,
    PromptRole,
    PromptUsage,
    RagItem,
)

logger = logging.getLogger(__name__)

CHARACTER_SECTIONS = (
    ("キャラクター概要", "description"),
    ("性格と話し方", "personality"),
    ("関係と世界観", "scenario"),
    ("応答方針", "system_prompt"),
    ("会話例", "mes_example"),
)
RAG_HEADING = "## 関連する記憶"


class TokenCounter(Protocol):
    def count(self, text: str) -> int:
        ...


@dataclass(frozen=True, repr=False)
class _SelectedRegions:
    character: PromptMessage | None
    rag: tuple[PromptMessage, ...]
    history: tuple[PromptMessage, ...]
    current_user: PromptMessage
    post_history: PromptMessage | None
    omitted_rag_items: int
    omitted_history_exchanges: int


class PromptBuilder:
    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def build(self, prompt_input: PromptBuildInput) -> BuiltPrompt:
        selected = self._select_regions(prompt_input)
        selected = self._fit_total_budget(selected, prompt_input.budget.total)
        messages = self._messages(selected)
        usage = self._usage(selected)
        logger.debug(
            "Prompt built: messages=%d tokens=%d omitted_rag=%d "
            "omitted_history=%d",
            len(messages),
            usage.total,
            usage.omitted_rag_items,
            usage.omitted_history_exchanges,
        )
        return BuiltPrompt(messages=messages, usage=usage)

    def _select_regions(self, prompt_input: PromptBuildInput) -> _SelectedRegions:
        character = self._character_message(prompt_input)
        current_user = PromptMessage(
            PromptRole.USER,
            prompt_input.current_user.content,
        )
        self._require_within(
            "current_user",
            self._message_tokens(current_user),
            prompt_input.budget.current_user,
        )
        history, omitted_history = self._select_history(prompt_input)
        rag, omitted_rag = self._select_rag(prompt_input)
        post_history = self._post_history_message(prompt_input)
        return _SelectedRegions(
            character=character,
            rag=rag,
            history=history,
            current_user=current_user,
            post_history=post_history,
            omitted_rag_items=omitted_rag,
            omitted_history_exchanges=omitted_history,
        )

    def _character_message(
        self,
        prompt_input: PromptBuildInput,
    ) -> PromptMessage | None:
        sections = [
            f"## {heading}\n{value.strip()}"
            for heading, field in CHARACTER_SECTIONS
            if (value := getattr(prompt_input.character, field)).strip()
        ]
        if not sections:
            return None
        message = PromptMessage(PromptRole.SYSTEM, "\n\n".join(sections))
        self._require_within(
            "character",
            self._message_tokens(message),
            prompt_input.budget.character,
        )
        return message

    def _select_rag(
        self,
        prompt_input: PromptBuildInput,
    ) -> tuple[tuple[PromptMessage, ...], int]:
        selected: list[PromptMessage] = []
        used = 0
        for item in prompt_input.rag.items:
            message = self._rag_message(item)
            item_tokens = self._message_tokens(message)
            if used + item_tokens > prompt_input.budget.rag:
                break
            selected.append(message)
            used += item_tokens
        return tuple(selected), len(prompt_input.rag.items) - len(selected)

    def _select_history(
        self,
        prompt_input: PromptBuildInput,
    ) -> tuple[tuple[PromptMessage, ...], int]:
        exchanges = prompt_input.history.exchanges
        if not exchanges:
            return (), 0
        latest = self._exchange_messages(exchanges[-1])
        latest_tokens = self._tokens(latest)
        self._require_within(
            "history",
            latest_tokens,
            prompt_input.budget.history,
        )
        selected = list(latest)
        used = latest_tokens
        selected_exchanges = 1
        for exchange in reversed(exchanges[:-1]):
            messages = self._exchange_messages(exchange)
            exchange_tokens = self._tokens(messages)
            if used + exchange_tokens > prompt_input.budget.history:
                break
            selected[0:0] = messages
            used += exchange_tokens
            selected_exchanges += 1
        return tuple(selected), len(exchanges) - selected_exchanges

    def _post_history_message(
        self,
        prompt_input: PromptBuildInput,
    ) -> PromptMessage | None:
        content = prompt_input.character.post_history_instructions.strip()
        if not content:
            return None
        message = PromptMessage(PromptRole.SYSTEM, content)
        if self._message_tokens(message) > prompt_input.budget.post_history:
            return None
        return message

    def _fit_total_budget(
        self,
        selected: _SelectedRegions,
        total_limit: int,
    ) -> _SelectedRegions:
        required = self._required_tokens(selected)
        self._require_within("total", required, total_limit)
        current = selected
        while current.rag and self._region_tokens(current) > total_limit:
            current = self._without_lowest_priority_rag(current)
        while (
            len(current.history) > 2
            and self._region_tokens(current) > total_limit
        ):
            current = self._without_oldest_exchange(current)
        if (
            current.post_history is not None
            and self._region_tokens(current) > total_limit
        ):
            current = self._without_post_history(current)
        return current

    def _usage(self, selected: _SelectedRegions) -> PromptUsage:
        character = self._optional_message_tokens(selected.character)
        rag = self._tokens(selected.rag)
        history = self._tokens(selected.history)
        current_user = self._message_tokens(selected.current_user)
        post_history = self._optional_message_tokens(selected.post_history)
        return PromptUsage(
            total=character + rag + history + current_user + post_history,
            character=character,
            rag=rag,
            history=history,
            current_user=current_user,
            post_history=post_history,
            omitted_rag_items=selected.omitted_rag_items,
            omitted_history_exchanges=selected.omitted_history_exchanges,
        )

    def _messages(
        self,
        selected: _SelectedRegions,
    ) -> tuple[PromptMessage, ...]:
        messages: list[PromptMessage] = []
        if selected.character is not None:
            messages.append(selected.character)
        messages.extend(selected.rag)
        messages.extend(selected.history)
        messages.append(selected.current_user)
        if selected.post_history is not None:
            messages.append(selected.post_history)
        return tuple(messages)

    def _rag_message(self, item: RagItem) -> PromptMessage:
        return PromptMessage(PromptRole.SYSTEM, f"{RAG_HEADING}\n{item.content}")

    @staticmethod
    def _exchange_messages(
        exchange: MaskedHistoryExchange,
    ) -> list[PromptMessage]:
        return [
            PromptMessage(PromptRole.USER, exchange.user_content),
            PromptMessage(PromptRole.ASSISTANT, exchange.assistant_content),
        ]

    def _required_tokens(self, selected: _SelectedRegions) -> int:
        return (
            self._optional_message_tokens(selected.character)
            + self._tokens(selected.history[-2:])
            + self._message_tokens(selected.current_user)
        )

    def _region_tokens(self, selected: _SelectedRegions) -> int:
        return self._usage(selected).total

    def _tokens(self, messages: tuple[PromptMessage, ...] | list[PromptMessage]) -> int:
        return sum(self._message_tokens(message) for message in messages)

    def _message_tokens(self, message: PromptMessage) -> int:
        return self._token_counter.count(message.content)

    def _optional_message_tokens(self, message: PromptMessage | None) -> int:
        return 0 if message is None else self._message_tokens(message)

    @staticmethod
    def _require_within(region: str, used: int, limit: int) -> None:
        if used > limit:
            raise PromptInputLimitError(region, used, limit)

    @staticmethod
    def _without_lowest_priority_rag(
        selected: _SelectedRegions,
    ) -> _SelectedRegions:
        return _SelectedRegions(
            character=selected.character,
            rag=selected.rag[:-1],
            history=selected.history,
            current_user=selected.current_user,
            post_history=selected.post_history,
            omitted_rag_items=selected.omitted_rag_items + 1,
            omitted_history_exchanges=selected.omitted_history_exchanges,
        )

    @staticmethod
    def _without_oldest_exchange(
        selected: _SelectedRegions,
    ) -> _SelectedRegions:
        return _SelectedRegions(
            character=selected.character,
            rag=selected.rag,
            history=selected.history[2:],
            current_user=selected.current_user,
            post_history=selected.post_history,
            omitted_rag_items=selected.omitted_rag_items,
            omitted_history_exchanges=selected.omitted_history_exchanges + 1,
        )

    @staticmethod
    def _without_post_history(
        selected: _SelectedRegions,
    ) -> _SelectedRegions:
        return _SelectedRegions(
            character=selected.character,
            rag=selected.rag,
            history=selected.history,
            current_user=selected.current_user,
            post_history=None,
            omitted_rag_items=selected.omitted_rag_items,
            omitted_history_exchanges=selected.omitted_history_exchanges,
        )
