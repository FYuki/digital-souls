from dataclasses import dataclass

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.screen_perception.provenance import ScreenLineage


@dataclass(frozen=True, repr=False)
class RestoredHistoryTurn:
    user_content: str
    assistant_content: str | None
    is_completed: bool
    screen_lineages: tuple[ScreenLineage, ...] = ()


def restore_prompt_turn(
    turn: ConversationTurn,
    screen_lineages: tuple[ScreenLineage, ...] = (),
) -> RestoredHistoryTurn:
    if turn.status not in {
        TurnStatus.COMPLETED,
        TurnStatus.INTERRUPTED,
        TurnStatus.FAILED,
    }:
        raise ValueError("only persisted content turns can be restored")
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
        screen_lineages=screen_lineages,
    )
