from uuid import UUID

from app.conversation_history.models import Conversation, ConversationTurn
from app.conversation_history.repository import ConversationHistoryRepository


class ConversationLifecycleService:
    def __init__(self, repository: ConversationHistoryRepository) -> None:
        self._repository = repository

    def create_conversation(self, character_id: str) -> Conversation:
        return self._repository.create_conversation(character_id)

    def list_active_conversations(self, character_id: str) -> list[Conversation]:
        return self._repository.list_active_conversations(character_id)

    def list_archived_conversations(self, character_id: str) -> list[Conversation]:
        return self._repository.list_archived_conversations(character_id)

    def list_conversation_turns(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> list[ConversationTurn]:
        return self._repository.list_history_turns(character_id, conversation_id)

    def archive_conversation(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> Conversation:
        return self._repository.archive_conversation(character_id, conversation_id)

    def unarchive_conversation(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> Conversation:
        return self._repository.unarchive_conversation(character_id, conversation_id)

    def hard_delete_conversation(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> None:
        self._repository.hard_delete_conversation(character_id, conversation_id)
