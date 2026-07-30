import logging
from dataclasses import dataclass

from app.characters.models import CharacterCardData
from app.prompting.types import (
    BuiltPrompt,
    CurrentUserOriginalText,
    PersistedConversationMessage,
    PromptMessage,
    PromptTokenBudget,
    RagContextText,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PromptParts:
    character: tuple[PromptMessage, ...]
    rag: tuple[PromptMessage, ...]
    history: tuple[PromptMessage, ...]
    current_user: tuple[PromptMessage, ...]
    final_instructions: tuple[PromptMessage, ...]

    @property
    def messages(self) -> tuple[PromptMessage, ...]:
        return (
            self.character
            + self.rag
            + self.history
            + self.current_user
            + self.final_instructions
        )


class PromptBuilder:
    def build(
        self,
        *,
        character: CharacterCardData,
        rag_context: tuple[RagContextText, ...],
        persisted_history: tuple[PersistedConversationMessage, ...],
        current_user_original_text: CurrentUserOriginalText,
        post_history_instructions: str,
        token_budget: PromptTokenBudget,
    ) -> BuiltPrompt:
        parts = _PromptParts(
            character=self._character_messages(character),
            rag=self._rag_messages(rag_context),
            history=self._history_messages(persisted_history),
            current_user=(
                PromptMessage(role="user", content=current_user_original_text),
            ),
            final_instructions=self._final_instruction_messages(
                post_history_instructions
            ),
        )
        messages = parts.messages
        logger.debug(
            "Built prompt: message_count=%d rag_count=%d history_count=%d",
            len(messages),
            len(parts.rag),
            len(parts.history),
        )
        return BuiltPrompt(messages=messages, token_budget=token_budget)

    @staticmethod
    def _character_messages(
        character: CharacterCardData,
    ) -> tuple[PromptMessage, ...]:
        content = "\n\n".join(
            value.strip()
            for value in (
                character.description,
                character.personality,
                character.scenario,
                character.system_prompt,
                character.mes_example,
            )
            if value.strip()
        )
        if not content:
            return ()
        return (PromptMessage(role="system", content=content),)

    @staticmethod
    def _rag_messages(
        rag_context: tuple[RagContextText, ...],
    ) -> tuple[PromptMessage, ...]:
        return tuple(
            PromptMessage(role="system", content=value.strip())
            for value in rag_context
            if value.strip()
        )

    @staticmethod
    def _history_messages(
        history: tuple[PersistedConversationMessage, ...],
    ) -> tuple[PromptMessage, ...]:
        if len(history) % 2:
            raise ValueError(
                "persisted history must contain complete user/assistant turns"
            )
        for index in range(0, len(history), 2):
            user_message, assistant_message = history[index : index + 2]
            if (
                user_message.role != "user"
                or assistant_message.role != "assistant"
                or not user_message.content.strip()
                or not assistant_message.content.strip()
            ):
                raise ValueError(
                    "persisted history must contain complete user/assistant turns"
                )
        return tuple(
            PromptMessage(role=message.role, content=message.content)
            for message in history
        )

    @staticmethod
    def _final_instruction_messages(
        instructions: str,
    ) -> tuple[PromptMessage, ...]:
        content = instructions.strip()
        if not content:
            return ()
        return (PromptMessage(role="system", content=content),)
