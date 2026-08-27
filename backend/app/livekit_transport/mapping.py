from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _Binding:
    core_participant_id: str
    participant_sid: str
    room_sid: str


@dataclass(frozen=True)
class ConnectionReplacement:
    disconnected_participant_sid: str
    active_participant_sid: str
    session_ended: bool


class ParticipantMapping:
    def __init__(self) -> None:
        self._bindings: dict[str, _Binding] = {}

    def bind(
        self,
        *,
        core_participant_id: str,
        identity: str,
        participant_sid: str,
        room_sid: str,
    ) -> None:
        self._bindings[identity] = _Binding(
            core_participant_id, participant_sid, room_sid
        )

    def core_notification(self, *, identity: str, event_type: str, **_: object) -> dict[str, str]:
        binding = self._bindings[identity]
        return {"participant_id": binding.core_participant_id, "type": event_type}

    def replace_connection(self, identity: str, participant_sid: str) -> ConnectionReplacement:
        binding = self._bindings[identity]
        previous_sid = binding.participant_sid
        binding.participant_sid = participant_sid
        return ConnectionReplacement(previous_sid, participant_sid, False)

    def participant_sid(self, identity: str) -> str | None:
        binding = self._bindings.get(identity)
        return None if binding is None else binding.participant_sid

    def clear(self) -> None:
        self._bindings.clear()
