"""Content-free, storage-independent per-message confirmation state."""

import json

from .application import CoreError
from .contracts import Message

type ConfirmationState = dict[str, bool | None]


def pending(indices: tuple[int, ...], messages: tuple[Message, ...]) -> ConfirmationState:
    if len(set(indices)) != len(indices) or any(
        type(index) is not int
        or not 0 <= index < len(messages) - 1
        or messages[index].role != "user"
        for index in indices
    ):
        raise CoreError(400, "invalid_confirmation", "Invalid confirmation indices")
    return {str(index): None for index in indices}


def decode(raw: str) -> ConfirmationState:
    value = json.loads(raw)
    if not isinstance(value, dict) or any(
        not isinstance(index, str)
        or not index.isascii()
        or not index.isdecimal()
        or str(int(index)) != index
        or (answer is not None and type(answer) is not bool)
        for index, answer in value.items()
    ):
        raise ValueError("invalid stored confirmation state")
    return value


def blocked(state: ConfirmationState, index: int) -> bool:
    return str(index) in state and state[str(index)] is not False


def resolve(state: ConfirmationState, index: int, accept: bool) -> ConfirmationState:
    key = str(index)
    if key not in state or state[key] is not None:
        raise CoreError(409, "confirmation_conflict", "Confirmation is not pending")
    return {**state, key: accept}
