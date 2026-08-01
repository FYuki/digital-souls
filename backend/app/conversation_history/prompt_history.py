from dataclasses import dataclass

from app.conversation_history.models import ConversationTurn, TurnStatus


@dataclass(frozen=True, repr=False)
class RestoredHistoryTurn:
    user_content: str
    assistant_content: str | None
    is_completed: bool


def restore_prompt_turn(turn: ConversationTurn) -> RestoredHistoryTurn:
    if turn.status not in {TurnStatus.COMPLETED, TurnStatus.FAILED}:
        raise ValueError("only completed or failed turns can be restored")
    if turn.user_content is None:
        raise ValueError("restored history turn requires saved user content")
    assistant_content = (
        None
        if turn.status is TurnStatus.FAILED and turn.assistant_content == ""
        else turn.assistant_content
    )
    return RestoredHistoryTurn(
        user_content=turn.user_content,
        assistant_content=assistant_content,
        is_completed=turn.status is TurnStatus.COMPLETED,
    )
