from dataclasses import dataclass

from app.characters.lore_selector import SelectedCharacterLore
from app.prompting.measurement import TokenCounter
from app.prompting.models import PromptMessage, PromptRole

CHARACTER_LORE_HEADING = "## キャラクターLore"


def character_lore_messages(
    entries: tuple[SelectedCharacterLore, ...],
) -> tuple[PromptMessage, ...]:
    return tuple(
        PromptMessage(
            PromptRole.SYSTEM,
            f"{CHARACTER_LORE_HEADING}\n{entry.content}",
        )
        for entry in entries
    )


@dataclass(frozen=True)
class PromptCharacterLoreTokenCounter:
    token_counter: TokenCounter

    def count_lore_tokens(
        self,
        entries: tuple[SelectedCharacterLore, ...],
    ) -> int:
        return self.token_counter.count_input_tokens(character_lore_messages(entries))
