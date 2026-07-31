import logging
from dataclasses import dataclass, replace
from typing import Protocol

from app.prompting.history import select_history, turn_messages
from app.prompting.models import (
    BuiltPrompt,
    MaskedHistoryTurn,
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
    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        ...


@dataclass(frozen=True, repr=False)
class _SelectedRegions:
    character: PromptMessage | None
    rag: tuple[PromptMessage, ...]
    history: tuple[MaskedHistoryTurn, ...]
    current_user: PromptMessage
    post_history: PromptMessage | None
    omitted_rag_items: int
    omitted_history_turns: int


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
        current_user = PromptMessage(PromptRole.USER, prompt_input.current_user.content)
        self._require_within(
            "current_user",
            self._tokens((current_user,)),
            prompt_input.budget.current_user,
        )
        selected_history = select_history(
            reversed(prompt_input.history.turns),
            token_counter=self._token_counter,
            token_limit=prompt_input.budget.history,
        )
        rag, omitted_rag = self._select_rag(prompt_input)
        return _SelectedRegions(
            character=character,
            rag=rag,
            history=selected_history.turns,
            current_user=current_user,
            post_history=self._post_history_message(prompt_input),
            omitted_rag_items=omitted_rag,
            omitted_history_turns=(
                prompt_input.history.omitted_turns
                + selected_history.omitted_turns
            ),
        )

    def _character_message(self, prompt_input: PromptBuildInput) -> PromptMessage | None:
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
            self._tokens((message,)),
            prompt_input.budget.character,
        )
        return message

    def _select_rag(
        self,
        prompt_input: PromptBuildInput,
    ) -> tuple[tuple[PromptMessage, ...], int]:
        selected: list[PromptMessage] = []
        for item in prompt_input.rag.items:
            candidate = (*selected, self._rag_message(item))
            if self._tokens(candidate) > prompt_input.budget.rag:
                break
            selected = list(candidate)
        return tuple(selected), len(prompt_input.rag.items) - len(selected)

    def _post_history_message(
        self,
        prompt_input: PromptBuildInput,
    ) -> PromptMessage | None:
        content = prompt_input.character.post_history_instructions.strip()
        if not content:
            return None
        message = PromptMessage(PromptRole.SYSTEM, content)
        if self._tokens((message,)) > prompt_input.budget.post_history:
            return None
        return message

    def _fit_total_budget(
        self,
        selected: _SelectedRegions,
        total_limit: int,
    ) -> _SelectedRegions:
        required = self._required_messages(selected)
        self._require_within("total", self._tokens(required), total_limit)
        current = selected
        while self._tokens(self._messages(current)) > total_limit:
            if current.rag:
                current = replace(
                    current,
                    rag=current.rag[:-1],
                    omitted_rag_items=current.omitted_rag_items + 1,
                )
                continue
            removable = self._oldest_removable_history_index(current.history)
            if removable is not None:
                current = replace(
                    current,
                    history=(
                        current.history[:removable]
                        + current.history[removable + 1 :]
                    ),
                    omitted_history_turns=current.omitted_history_turns + 1,
                )
                continue
            if current.post_history is not None:
                current = replace(current, post_history=None)
                continue
            self._require_within(
                "total",
                self._tokens(self._messages(current)),
                total_limit,
            )
        return current

    def _usage(self, selected: _SelectedRegions) -> PromptUsage:
        character = self._tokens_optional(selected.character)
        rag = self._tokens(selected.rag)
        history = self._tokens(self._history_messages(selected.history))
        current_user = self._tokens((selected.current_user,))
        post_history = self._tokens_optional(selected.post_history)
        return PromptUsage(
            total=self._tokens(self._messages(selected)),
            character=character,
            rag=rag,
            history=history,
            current_user=current_user,
            post_history=post_history,
            omitted_rag_items=selected.omitted_rag_items,
            omitted_history_exchanges=selected.omitted_history_turns,
        )

    def _messages(self, selected: _SelectedRegions) -> tuple[PromptMessage, ...]:
        messages: list[PromptMessage] = []
        if selected.character is not None:
            messages.append(selected.character)
        messages.extend(selected.rag)
        messages.extend(self._history_messages(selected.history))
        if selected.post_history is not None:
            messages.append(selected.post_history)
        messages.append(selected.current_user)
        return tuple(messages)

    def _required_messages(
        self,
        selected: _SelectedRegions,
    ) -> tuple[PromptMessage, ...]:
        messages: list[PromptMessage] = []
        if selected.character is not None:
            messages.append(selected.character)
        latest_completed = next(
            (turn for turn in reversed(selected.history) if turn.is_completed),
            None,
        )
        if latest_completed is not None:
            messages.extend(turn_messages(latest_completed))
        messages.append(selected.current_user)
        return tuple(messages)

    @staticmethod
    def _oldest_removable_history_index(
        turns: tuple[MaskedHistoryTurn, ...],
    ) -> int | None:
        protected = next(
            (index for index in range(len(turns) - 1, -1, -1) if turns[index].is_completed),
            None,
        )
        return next(
            (index for index in range(len(turns)) if index != protected),
            None,
        )

    @staticmethod
    def _history_messages(
        turns: tuple[MaskedHistoryTurn, ...],
    ) -> tuple[PromptMessage, ...]:
        return tuple(message for turn in turns for message in turn_messages(turn))

    def _tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        if not messages:
            return 0
        return self._token_counter.count_input_tokens(messages)

    def _tokens_optional(self, message: PromptMessage | None) -> int:
        return 0 if message is None else self._tokens((message,))

    @staticmethod
    def _rag_message(item: RagItem) -> PromptMessage:
        return PromptMessage(PromptRole.SYSTEM, f"{RAG_HEADING}\n{item.content}")

    @staticmethod
    def _require_within(region: str, used: int, limit: int) -> None:
        if used > limit:
            raise PromptInputLimitError(region, used, limit)
