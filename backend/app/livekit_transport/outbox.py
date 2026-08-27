from __future__ import annotations


class OutboxCapacityExceeded(RuntimeError):
    pass


class InMemoryOutbox:
    def __init__(self, *, max_events: int, max_bytes: int) -> None:
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._events: dict[str, bytes] = {}
        self.transport_available = True

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def byte_count(self) -> int:
        return sum(len(payload) for payload in self._events.values())

    def enqueue(self, event_id: str, payload: bytes) -> None:
        if (
            self.event_count + 1 > self._max_events
            or self.byte_count + len(payload) > self._max_bytes
        ):
            self.transport_available = False
            raise OutboxCapacityExceeded("outbox capacity exceeded")
        self._events[event_id] = payload

    def ack(self, event_id: str) -> bool:
        return self._events.pop(event_id, None) is not None

    def contains(self, event_id: str) -> bool:
        return event_id in self._events

    def pending(self) -> list[tuple[str, bytes]]:
        return list(self._events.items())

    def clear(self) -> None:
        self._events.clear()


class InMemoryOutboxManager:
    def __init__(self, *, max_events: int, max_bytes: int) -> None:
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._outboxes: dict[tuple[str, str], InMemoryOutbox] = {}

    def get(self, session_id: str, direction: str) -> InMemoryOutbox:
        key = (session_id, direction)
        if key not in self._outboxes:
            self._outboxes[key] = InMemoryOutbox(
                max_events=self._max_events, max_bytes=self._max_bytes
            )
        return self._outboxes[key]

    def clear_session(self, session_id: str) -> None:
        owned_keys = [
            key for key in self._outboxes if key[0] == session_id
        ]
        for key in owned_keys:
            self._outboxes[key].clear()
            del self._outboxes[key]
