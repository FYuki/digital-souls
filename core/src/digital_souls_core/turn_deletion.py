"""Select whole turns using only structured tool call/result relationships."""

from collections.abc import Mapping
from typing import Literal

from .contracts import Message


def select_turns(
    turns: Mapping[int, tuple[Message, ...]],
    turn_revision: int,
    scope: Literal["selected", "following"],
) -> tuple[int, ...]:
    selected = {
        revision
        for revision in turns
        if revision == turn_revision or (scope == "following" and revision > turn_revision)
    }
    calls: dict[str, set[int]] = {}
    results: dict[str, set[int]] = {}
    for revision, messages in turns.items():
        for message in messages:
            if message.role == "assistant":
                for call in message.tool_calls or ():
                    calls.setdefault(call.id, set()).add(revision)
            elif message.role == "tool" and message.tool_call_id is not None:
                results.setdefault(message.tool_call_id, set()).add(revision)
    connections = [
        calls[identifier] | results[identifier] for identifier in calls.keys() & results.keys()
    ]
    while True:
        expanded = selected.union(*(group for group in connections if group & selected))
        if expanded == selected:
            return tuple(sorted(selected))
        selected = expanded
