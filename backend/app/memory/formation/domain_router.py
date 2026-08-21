from typing import Protocol

from app.conversation_history.models import ConversationTurn


class DomainRecordRouter(Protocol):
    def dispatch(self, turn: ConversationTurn) -> None: ...


class NoOpDomainRecordRouter:
    def dispatch(self, turn: ConversationTurn) -> None:
        return None
