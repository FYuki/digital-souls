import json
from pathlib import Path

from app.characters.models import CharacterCard, CharacterCardData
from app.prompting.types import BuiltPrompt

def character_card(
    system_prompt: str,
    *,
    first_mes: str = "",
) -> CharacterCard:
    return CharacterCard(
        spec="chara_card_v3",
        spec_version="3.0",
        data=CharacterCardData(
            name="テスト人格",
            description=system_prompt,
            personality="",
            scenario="",
            first_mes=first_mes,
            mes_example="",
            creator_notes="",
            system_prompt="",
            post_history_instructions="",
            alternate_greetings=(),
            group_only_greetings=(),
            creator="",
            character_version="",
            tags=(),
            extensions={},
            extra_fields={},
        ),
        extra_fields={},
    )


def prompt_messages(prompt: BuiltPrompt) -> list[tuple[str, str]]:
    return [(message.role, message.content) for message in prompt.messages]


def current_user_text(prompt: BuiltPrompt) -> str:
    user_messages = [
        message.content for message in prompt.messages if message.role == "user"
    ]
    return user_messages[-1]


def write_character_card(
    root: Path,
    character: str,
    system_prompt: str,
) -> None:
    character_dir = root / "characters" / character
    character_dir.mkdir(parents=True)
    data = {
        "name": character,
        "description": system_prompt,
        "personality": "",
        "scenario": "",
        "first_mes": "",
        "mes_example": "",
        "creator_notes": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [],
        "group_only_greetings": [],
        "creator": "",
        "character_version": "",
        "tags": [],
        "extensions": {},
    }
    card = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": data,
    }
    character_dir.joinpath(f"{character}.card.json").write_text(
        json.dumps(card, ensure_ascii=False),
        encoding="utf-8",
    )
