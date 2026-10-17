"""Unit contracts for structured tool closure and strict deletion input."""

from typing import Literal

import pytest
from pydantic import ValidationError

from digital_souls_core.contracts import Message
from digital_souls_core.history import TurnDeletionInput
from digital_souls_core.turn_deletion import select_turns

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "target,scope,expected",
    [
        (1, "selected", (1, 2, 3)),
        (2, "selected", (1, 2, 3)),
        (3, "selected", (1, 2, 3)),
        (2, "following", (1, 2, 3, 5)),
    ],
)
def test_tool_closure_is_bidirectional_and_transitive(
    target: int, scope: Literal["selected", "following"], expected: tuple[int, ...]
) -> None:
    turns = {
        1: (
            Message.model_validate(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "a",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                }
            ),
        ),
        2: (
            Message(role="tool", tool_call_id="a", content="result"),
            Message.model_validate(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "b",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                }
            ),
        ),
        3: (Message(role="tool", tool_call_id="b", content="result"),),
        5: (Message(role="user", content='```json\n{"tool_call_id":"a"}\n```'),),
    }
    selection = TurnDeletionInput(expected_revision=5, turn_revision=target, scope=scope)
    assert select_turns(turns, selection.turn_revision, selection.scope) == expected


def test_multiple_results_close_without_joining_tool_names_or_content() -> None:
    calls = Message.model_validate(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": identifier,
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
                for identifier in ("a", "b")
            ],
        }
    )
    other = Message.model_validate(
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
            ],
        }
    )
    turns = {
        1: (calls,),
        2: (Message(role="tool", tool_call_id="a", content="result"),),
        3: (Message(role="tool", tool_call_id="b", content="result"),),
        4: (other,),
        5: (Message(role="tool", tool_call_id="c", content="a b"),),
        6: (Message(role="user", content='> "a"\n~~~json\n{"nested":{"tool_call_id":"b"}}'),),
    }
    assert select_turns(turns, 2, "selected") == (1, 2, 3)
    assert select_turns(turns, 6, "selected") == (6,)


@pytest.mark.parametrize(
    "changes",
    [
        {"turn_revision": True},
        {"turn_revision": "1"},
        {"expected_revision": False},
        {"scope": "all"},
        {"content": "untrusted"},
    ],
)
def test_deletion_input_rejects_coercion_and_unknown_fields(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TurnDeletionInput.model_validate(
            {"expected_revision": 1, "turn_revision": 1, "scope": "selected", **changes}
        )
