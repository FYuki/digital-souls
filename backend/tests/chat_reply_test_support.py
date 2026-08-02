from uuid import UUID

from app.chat_service import ChatReply, PersistedContentTurn


def persisted_reply(response: str, turn_id: UUID) -> ChatReply:
    return ChatReply(
        turn_id=turn_id,
        persisted_turn=PersistedContentTurn(
            turn_id=turn_id,
            user_content="saved user content",
            assistant_content=response,
        ),
    )
