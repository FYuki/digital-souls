import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, Unpack
from uuid import UUID, uuid4

from app.characters.models import CharacterCard, CharacterCardData
from app.conversation_history.models import (
    Conversation,
    ConversationTurn,
    PersistedMaskedText,
    PrivacySkipReason,
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
    TurnStatus,
)
from app.prompting.types import BuiltPrompt

TEST_CONVERSATION_ID = UUID("00000000-0000-4000-8000-000000000001")


class _TurnChanges(TypedDict, total=False):
    user_content: PersistedMaskedText | None
    assistant_content: PersistedMaskedText | None
    status: TurnStatus
    privacy_reason_code: PrivacySkipReason | None


class StubConversationHistory:
    def __init__(self, turns: list[ConversationTurn] | None = None) -> None:
        self._turns = [] if turns is None else turns

    def create_conversation(self, character_id: str) -> Conversation:
        return Conversation(
            character_id=character_id,
            conversation_id=TEST_CONVERSATION_ID,
            created_at=datetime.now(UTC),
        )

    def resume_conversation(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> Conversation:
        assert conversation_id == TEST_CONVERSATION_ID
        return Conversation(
            character_id=character_id,
            conversation_id=conversation_id,
            created_at=datetime.now(UTC),
        )

    def list_turns(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> list[ConversationTurn]:
        assert conversation_id == TEST_CONVERSATION_ID
        return [
            turn
            for turn in self._turns
            if turn.character_id == character_id
            and turn.conversation_id == conversation_id
        ]

    def create_processing_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_input: ProcessingTurnInput,
    ) -> ConversationTurn:
        now = datetime.now(UTC)
        turn = ConversationTurn(
            turn_id=uuid4(),
            character_id=character_id,
            conversation_id=conversation_id,
            user_content=turn_input.sanitized_user_content,
            assistant_content=None,
            status=TurnStatus.PROCESSING,
            privacy_reason_code=None,
            created_at=now,
            updated_at=now,
        )
        self._turns.append(turn)
        return turn

    def complete_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        sanitized_assistant_content: PersistedMaskedText,
    ) -> ConversationTurn:
        return self._replace_turn(
            character_id,
            conversation_id,
            turn_id,
            assistant_content=sanitized_assistant_content,
            status=TurnStatus.COMPLETED,
        )

    def create_privacy_skipped_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_input: PrivacySkippedTurnInput,
    ) -> ConversationTurn:
        now = datetime.now(UTC)
        turn = ConversationTurn(
            turn_id=uuid4(),
            character_id=character_id,
            conversation_id=conversation_id,
            user_content=None,
            assistant_content=None,
            status=TurnStatus.PRIVACY_SKIPPED,
            privacy_reason_code=turn_input.reason_code,
            created_at=now,
            updated_at=now,
        )
        self._turns.append(turn)
        return turn

    def privacy_skip_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        turn_input: PrivacySkippedTurnInput,
    ) -> ConversationTurn:
        return self._replace_turn(
            character_id,
            conversation_id,
            turn_id,
            user_content=None,
            assistant_content=None,
            status=TurnStatus.PRIVACY_SKIPPED,
            privacy_reason_code=turn_input.reason_code,
        )

    def fail_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> ConversationTurn:
        return self._replace_turn(
            character_id,
            conversation_id,
            turn_id,
            status=TurnStatus.FAILED,
        )

    def _replace_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        **changes: Unpack[_TurnChanges],
    ) -> ConversationTurn:
        index = next(
            index
            for index, turn in enumerate(self._turns)
            if turn.character_id == character_id
            and turn.conversation_id == conversation_id
            and turn.turn_id == turn_id
        )
        updated = replace(
            self._turns[index],
            updated_at=datetime.now(UTC),
            **changes,
        )
        self._turns[index] = updated
        return updated


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
            extensions={},
        ),
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
