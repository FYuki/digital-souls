from __future__ import annotations

from typing import Callable, TypedDict


class _InProgressResponse(TypedDict):
    response_id: str
    confirmed_audio_sequence: int


class SessionLifecycle:
    def __init__(
        self,
        *,
        session_id: str,
        reconnect_grace_ms: int,
        cleanup: Callable[[str], None] | None = None,
        notify: Callable[[dict[str, object]], None] | None = None,
        created_at_ms: int = 0,
        join_deadline_ms: int = 90_000,
    ) -> None:
        self.session_id = session_id
        self.reconnect_grace_ms = reconnect_grace_ms
        self._cleanup = cleanup
        self._notify = notify
        self.created_at_ms = created_at_ms
        self.join_deadline_ms = join_deadline_ms
        self.phase = "bootstrapping"
        self.generation = 0
        self._reconnect_deadline_ms: int | None = None
        self._end_result: dict[str, str] | None = None
        self.in_progress_response: _InProgressResponse | None = None

    @property
    def session_ended(self) -> bool:
        return self.phase == "ended"

    def activate(self) -> None:
        self.phase = "available"

    def begin_response(self, *, response_id: str) -> None:
        self.in_progress_response = {
            "response_id": response_id,
            "confirmed_audio_sequence": 0,
        }

    def confirm_playback(
        self, *, response_id: str, confirmed_audio_sequence: int
    ) -> bool:
        response = self.in_progress_response
        if response is None or response["response_id"] != response_id:
            return False
        current_sequence = response["confirmed_audio_sequence"]
        if confirmed_audio_sequence <= current_sequence:
            return False
        self.in_progress_response = {
            "response_id": response_id,
            "confirmed_audio_sequence": confirmed_audio_sequence,
        }
        return True

    def disconnect(self, *, now_ms: int) -> None:
        self.phase = "unavailable"
        self._reconnect_deadline_ms = now_ms + self.reconnect_grace_ms
        self._interrupt_response()

    def reconnect(self, *, now_ms: int) -> None:
        if self._reconnect_deadline_ms is None or now_ms >= self._reconnect_deadline_ms:
            raise ValueError("reconnect deadline has expired")
        self._interrupt_response()
        self.phase = "available"
        self.generation += 1
        self._reconnect_deadline_ms = None

    def accepts_generation(self, generation: int) -> bool:
        return self.phase == "available" and generation == self.generation

    def advance_generation(self) -> None:
        if self.phase != "available":
            raise ValueError("session is not available")
        self._interrupt_response()
        self.generation += 1

    def _interrupt_response(self) -> None:
        response = self.in_progress_response
        if response is None:
            return
        self.in_progress_response = None
        if self._notify is not None:
            self._notify(
                {
                    "type": "response_interrupted",
                    "session_id": self.session_id,
                    **response,
                }
            )

    def expire(self, *, now_ms: int) -> bool:
        if self._reconnect_deadline_ms is None or now_ms < self._reconnect_deadline_ms:
            return False
        self._end_with_owned_cleanup("reconnect_timeout")
        return True

    def expire_never_joined(self, *, now_ms: int) -> bool:
        if self.phase != "bootstrapping" or now_ms < self.join_deadline_ms:
            return False
        self._end_with_owned_cleanup("join_token_expired")
        return True

    def end(self, reason: str) -> dict[str, str]:
        if self._end_result is not None:
            return self._end_result
        self.phase = "ended"
        if self._cleanup is not None:
            self._cleanup("cleanup")
        self._end_result = {
            "session_id": self.session_id,
            "phase": "ended",
            "reason": reason,
        }
        return self._end_result

    def _end_with_owned_cleanup(self, reason: str) -> None:
        if self.phase == "ended":
            return
        self.phase = "ended"
        if self._cleanup is not None:
            for resource in ("room", "runtime", "outbox", "mapping"):
                self._cleanup(resource)
        self._end_result = {
            "session_id": self.session_id,
            "phase": "ended",
            "reason": reason,
        }
