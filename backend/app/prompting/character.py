"""通常promptと開始準備で、固定人格部分の整形を共有する。"""
from app.prompting.models import CharacterPrompt, PromptMessage, PromptRole

CHARACTER_SECTIONS = (
    ("キャラクター概要", "description"),
    ("性格と話し方", "personality"),
    ("関係と世界観", "scenario"),
    ("応答方針", "system_prompt"),
    ("会話例", "mes_example"),
)


def character_system_message(character: CharacterPrompt) -> PromptMessage | None:
    sections = [
        f"## {heading}\n{value.strip()}"
        for heading, field in CHARACTER_SECTIONS
        if (value := getattr(character, field)).strip()
    ]
    return PromptMessage(PromptRole.SYSTEM, "\n\n".join(sections)) if sections else None


def post_history_message(character: CharacterPrompt) -> PromptMessage | None:
    content = character.post_history_instructions.strip()
    return PromptMessage(PromptRole.SYSTEM, content) if content else None


def fixed_character_messages(character: CharacterPrompt) -> tuple[PromptMessage, ...]:
    return tuple(message for message in (
        character_system_message(character), post_history_message(character),
    ) if message is not None)
