"""Per-message confirmation validation and state transitions."""

import pytest
from pydantic import ValidationError

from digital_souls_core.application import CoreError
from digital_souls_core.contracts import Message
from digital_souls_core.history import MemoryConfirmation
from digital_souls_core.memory_confirmation import blocked, decode, pending, resolve

pytestmark = pytest.mark.ut


def test_pending_decline_and_accept_are_independent() -> None:
    messages = (
        Message(role="user", content="Synthetic first"),
        Message(role="user", content="Synthetic second"),
        Message(role="assistant", content="Synthetic reply"),
    )
    state = pending((0, 1), messages)
    assert blocked(state, 0) and blocked(state, 1)
    assert not blocked(state, 2)
    declined = resolve(state, 0, False)
    assert not blocked(declined, 0) and blocked(declined, 1)
    accepted = resolve(declined, 1, True)
    assert blocked(accepted, 1)
    assert state == {"0": None, "1": None}
    assert decode('{"0":false,"1":true}') == accepted
    with pytest.raises(CoreError):
        resolve(accepted, 1, False)
    with pytest.raises(CoreError):
        resolve(state, 9, False)


@pytest.mark.parametrize("indices", [(0, 0), (-1,), (1,), (2,), (True,)])
def test_pending_rejects_invalid_source_indices(indices: tuple[int, ...]) -> None:
    with pytest.raises(CoreError):
        pending(
            indices,
            (Message(role="user", content="Synthetic"), Message(role="assistant", content="Reply")),
        )


def test_pending_rejects_tool_source() -> None:
    with pytest.raises(CoreError):
        pending(
            (0,),
            (
                Message(role="tool", content="Synthetic", tool_call_id="tool_1"),
                Message(role="assistant", content="Reply"),
            ),
        )


@pytest.mark.parametrize("raw", ["[]", "null", '{"-1":null}', '{"01":null}', '{"0":0}'])
def test_decode_rejects_invalid_stored_state(raw: str) -> None:
    with pytest.raises(ValueError):
        decode(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": -1},
        {"turn_revision": 0},
        {"message_index": -1},
        {"accept_private_mode": "false"},
        {"scope": "synthetic"},
    ],
)
def test_confirmation_input_is_strict(changes: dict[str, object]) -> None:
    body = {
        "expected_revision": 1,
        "turn_revision": 1,
        "message_index": 0,
        "accept_private_mode": False,
    }
    with pytest.raises(ValidationError):
        MemoryConfirmation.model_validate({**body, **changes})
