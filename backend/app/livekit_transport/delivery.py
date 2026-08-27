from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable, Protocol

from jsonschema import Draft202012Validator, FormatChecker


class TerminalProtocolError(RuntimeError):
    pass


class ConflictingDuplicateError(TerminalProtocolError):
    pass


class SequenceGapError(TerminalProtocolError):
    pass


@dataclass(frozen=True)
class DeduplicationResult:
    status: str
    payload: bytes


class EventDeduplicator:
    def __init__(self) -> None:
        self._payloads: dict[str, bytes] = {}

    def classify(self, event_id: str, payload: bytes) -> DeduplicationResult:
        previous = self._payloads.get(event_id)
        if previous is None:
            self._payloads[event_id] = payload
            return DeduplicationResult("accepted", payload)
        if previous != payload:
            raise ConflictingDuplicateError("event_id has a conflicting payload")
        return DeduplicationResult("duplicate", payload)


class EventSequenceTracker:
    def __init__(self) -> None:
        self._last: dict[tuple[str, str, str], int] = {}

    def accept(self, event: dict[str, object]) -> None:
        response_id = event.get("response_id")
        if not isinstance(response_id, str):
            return
        for field in ("text_sequence", "audio_sequence"):
            sequence = event.get(field)
            if not isinstance(sequence, int):
                continue
            key = (str(event["type"]), response_id, field)
            expected = self._last.get(key, 0) + 1
            if sequence != expected:
                raise SequenceGapError(
                    f"{field} must be contiguous: expected {expected}, got {sequence}"
                )
            self._last[key] = sequence


class CoreNotificationPort(Protocol):
    def notify(self, payload: bytes) -> None: ...


_CORE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "voice-session"
    / "voice-session.schema.json"
)
_CORE_VALIDATOR = Draft202012Validator(
    json.loads(_CORE_SCHEMA_PATH.read_text(encoding="utf-8")),
    format_checker=FormatChecker(),
)

_PRIVATE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "livekit-transport"
    / "livekit-transport.schema.json"
)
_PRIVATE_VALIDATOR = Draft202012Validator(
    json.loads(_PRIVATE_SCHEMA_PATH.read_text(encoding="utf-8")),
    format_checker=FormatChecker(),
)


def decode_core_event(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TerminalProtocolError("malformed Core event") from error
    if not isinstance(value, dict) or list(_CORE_VALIDATOR.iter_errors(value)):
        raise TerminalProtocolError("invalid Core event")
    return value


def decode_private_frame(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TerminalProtocolError("malformed transport frame") from error
    if not isinstance(value, dict) or list(_PRIVATE_VALIDATOR.iter_errors(value)):
        raise TerminalProtocolError("invalid transport frame")
    return value


class CoreEventDelivery:
    def __init__(
        self,
        *,
        core_port: CoreNotificationPort,
        cleanup: Callable[[str], None] | None = None,
    ) -> None:
        self._core_port = core_port
        self._cleanup = cleanup
        self._deduplicator = EventDeduplicator()
        self._sequences = EventSequenceTracker()

    def receive(
        self, payload: bytes, decoded_event: dict[str, object] | None = None
    ) -> None:
        session_id = self._session_id(payload)
        try:
            event = decoded_event if decoded_event is not None else decode_core_event(payload)
            event_id = event["event_id"]
            result = self._deduplicator.classify(event_id, payload)
            if result.status == "accepted":
                self._sequences.accept(event)
                self._core_port.notify(result.payload)
        except (ConflictingDuplicateError, TerminalProtocolError):
            if self._cleanup is not None:
                self._cleanup(session_id)
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            if self._cleanup is not None:
                self._cleanup(session_id)
            raise TerminalProtocolError("malformed Core event") from error

    @staticmethod
    def _session_id(payload: bytes) -> str:
        try:
            value = json.loads(payload).get("session_id")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return "unknown"
        return value if isinstance(value, str) else "unknown"


def retry_deadlines_ms(initially_sent_at_ms: int) -> tuple[int, int, int]:
    return (
        initially_sent_at_ms + 1_000,
        initially_sent_at_ms + 2_000,
        initially_sent_at_ms + 4_000,
    )


@dataclass(frozen=True)
class RetryAttempt:
    event_id: str
    payload: bytes


class RetryState:
    def __init__(self, *, event_id: str, payload: bytes, initially_sent_at_ms: int) -> None:
        self.event_id = event_id
        self.payload = payload
        self._deadlines = retry_deadlines_ms(initially_sent_at_ms)
        self._attempts = 0
        self.transport_available = True
        self.delivered = False

    def poll(self, now_ms: int) -> RetryAttempt | None:
        if self._attempts < len(self._deadlines) and now_ms >= self._deadlines[self._attempts]:
            self._attempts += 1
            return RetryAttempt(self.event_id, self.payload)
        if self._attempts == len(self._deadlines) and now_ms > self._deadlines[-1]:
            self.transport_available = False
        return None


def reconnect_sync_frames(
    *, authoritative_state: dict[str, object], terminal_outcomes: list[dict[str, object]]
) -> list[dict[str, object]]:
    return [
        {
            "type": "authoritative_state",
            **authoritative_state,
            "terminal_outcomes": list(terminal_outcomes),
        }
    ]
