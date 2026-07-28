from app.conversation_history.errors import InvalidStateTransitionError
from app.conversation_history.models import TurnStatus

ALLOWED_TURN_TRANSITIONS = frozenset(
    {
        (TurnStatus.PROCESSING, TurnStatus.COMPLETED),
        (TurnStatus.PROCESSING, TurnStatus.FAILED),
        (TurnStatus.COMPLETED, TurnStatus.FAILED),
    }
)


def require_turn_transition(
    current: TurnStatus,
    requested: TurnStatus,
) -> None:
    if (current, requested) not in ALLOWED_TURN_TRANSITIONS:
        raise InvalidStateTransitionError(current, requested)
