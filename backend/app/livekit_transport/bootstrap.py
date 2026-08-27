from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Callable, Mapping, Protocol
from uuid import UUID, uuid4

from app.conversation_history.errors import (
    ConversationCharacterBoundaryError,
    ConversationHistoryError,
)


BOOTSTRAP_TIMEOUT_SECONDS = 10
JOIN_TOKEN_TTL_SECONDS = 90
MAX_RECONNECT_GRACE_MS = 60_000


class BootstrapTimeoutError(RuntimeError):
    pass


class _RoomCleanupPendingError(RuntimeError):
    pass


class BootstrapConflictError(RuntimeError):
    status_code = 409


class UnknownSessionError(RuntimeError):
    status_code = 409


class BindingValidationError(RuntimeError):
    status_code = 409

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BindingValidator(Protocol):
    def validate(self, *, character_id: str, conversation_id: UUID) -> None: ...


class CharacterConversationBindingValidator:
    def __init__(self, *, character_loader: Callable[[str], object], conversations: object) -> None:
        self._character_loader = character_loader
        self._conversations = conversations

    def validate(self, *, character_id: str, conversation_id: UUID) -> None:
        try:
            self._character_loader(character_id)
        except (FileNotFoundError, ValueError) as error:
            raise BindingValidationError("character_not_found") from error
        try:
            conversation = self._conversations.resume_conversation(
                character_id, conversation_id
            )
        except ConversationCharacterBoundaryError as error:
            raise BindingValidationError("session_binding_mismatch") from error
        except (ConversationHistoryError, KeyError, LookupError, ValueError) as error:
            raise BindingValidationError("conversation_not_found") from error
        if getattr(conversation, "character_id", None) != character_id:
            raise BindingValidationError("session_binding_mismatch")
        if getattr(conversation, "archived_at", None) is not None:
            raise BindingValidationError("conversation_not_active")


class MonotonicClock(Protocol):
    def now_ms(self) -> int: ...


class _SystemMonotonicClock:
    def now_ms(self) -> int:
        return int(time.monotonic() * 1000)


@dataclass(frozen=True)
class SessionReservation:
    session_id: str
    request: Mapping[str, object]
    created_at_ms: int


class InMemorySessionBindingRepository:
    def __init__(
        self,
        *,
        session_id_factory: Callable[[], str] = lambda: str(uuid4()),
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        self._session_id_factory = session_id_factory
        self._clock = monotonic_clock or _SystemMonotonicClock()
        self._by_session: dict[str, SessionReservation] = {}
        self._by_request: dict[str, SessionReservation] = {}
        self._by_conversation: dict[str, SessionReservation] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, request: Mapping[str, object]) -> str:
        async with self._lock:
            request_id = str(request["request_id"])
            conversation_id = str(request["conversation_id"])
            previous = self._by_request.get(request_id)
            if previous is not None:
                if dict(previous.request) != dict(request):
                    raise BootstrapConflictError("request_id payload conflict")
                return previous.session_id
            active = self._by_conversation.get(conversation_id)
            if active is not None:
                raise BootstrapConflictError("conversation already has an active session")
            session_id = self._session_id_factory()
            reservation = SessionReservation(
                session_id=session_id,
                request=dict(request),
                created_at_ms=self._clock.now_ms(),
            )
            self._by_session[session_id] = reservation
            self._by_request[request_id] = reservation
            self._by_conversation[conversation_id] = reservation
            return session_id

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            reservation = self._by_session.pop(session_id, None)
            if reservation is None:
                return
            self._by_request.pop(str(reservation.request["request_id"]), None)
            self._by_conversation.pop(
                str(reservation.request["conversation_id"]), None
            )

    def contains(self, session_id: str) -> bool:
        return session_id in self._by_session

    def get(self, session_id: str) -> SessionReservation | None:
        return self._by_session.get(session_id)


@dataclass(frozen=True)
class BootstrapResult:
    session_id: str
    participant_id: str
    room: str
    token: str
    reconnect_grace_ms: int


class BootstrapService:
    def __init__(
        self,
        *,
        session_repository: object,
        room_manager: object,
        runtime_manager: object,
        token_signer: object,
        timeout_seconds: int,
        binding_validator: BindingValidator | None = None,
    ) -> None:
        self._sessions = session_repository
        self._rooms = room_manager
        self._runtimes = runtime_manager
        self._signer = token_signer
        self._timeout_seconds = timeout_seconds
        self._binding_validator = binding_validator
        self._completed: dict[str, tuple[dict[str, object], BootstrapResult]] = {}
        self._by_session: dict[str, BootstrapResult] = {}
        self._lock = asyncio.Lock()
        self._inflight: dict[
            str, tuple[dict[str, object], asyncio.Task[BootstrapResult]]
        ] = {}
        self._end_tasks: dict[str, asyncio.Task[None]] = {}

    async def bootstrap(self, raw_request: Mapping[str, object]) -> BootstrapResult:
        request = dict(raw_request)
        if self._binding_validator is not None:
            self._binding_validator.validate(
                character_id=str(request["character_id"]),
                conversation_id=UUID(str(request["conversation_id"])),
            )
        if "session_id" in request:
            session_id = str(request["session_id"])
            reservation = getattr(self._sessions, "get", lambda _value: None)(session_id)
            result = self._by_session.get(session_id)
            if reservation is None or result is None:
                raise UnknownSessionError("session is not owned by this process")
            if (
                str(reservation.request["character_id"]) != str(request["character_id"])
                or str(reservation.request["conversation_id"])
                != str(request["conversation_id"])
            ):
                raise BindingValidationError("session_binding_mismatch")
            token = await self._issue_user_token(session_id)
            return BootstrapResult(
                session_id=result.session_id,
                participant_id=result.participant_id,
                room=result.room,
                token=token,
                reconnect_grace_ms=result.reconnect_grace_ms,
            )
        request_id = str(request["request_id"])
        async with self._lock:
            completed = self._completed.get(request_id)
            if completed is not None:
                previous_request, previous_result = completed
                if not getattr(self._sessions, "contains", lambda _value: True)(
                    previous_result.session_id
                ):
                    self._completed.pop(request_id, None)
                    self._by_session.pop(previous_result.session_id, None)
                    completed = None
            if completed is not None:
                previous_request, previous_result = completed
                if previous_request != request:
                    raise BootstrapConflictError("request_id payload conflict")
                token = await self._issue_user_token(previous_result.session_id)
                return BootstrapResult(
                    session_id=previous_result.session_id,
                    participant_id=previous_result.participant_id,
                    room=previous_result.room,
                    token=token,
                    reconnect_grace_ms=previous_result.reconnect_grace_ms,
                )
            inflight = self._inflight.get(request_id)
            if inflight is not None:
                inflight_request, task = inflight
                if inflight_request != request:
                    raise BootstrapConflictError("request_id payload conflict")
            else:
                task = asyncio.create_task(self._bootstrap_once(request))
                self._inflight[request_id] = (request, task)
        try:
            result = await task
        finally:
            async with self._lock:
                if self._inflight.get(request_id, (None, None))[1] is task:
                    self._inflight.pop(request_id, None)
        async with self._lock:
            self._completed[request_id] = (request, result)
            self._by_session[result.session_id] = result
        return result

    async def end(self, session_id: str) -> None:
        async with self._lock:
            result = self._by_session.get(session_id)
            if result is None:
                return
            task = self._end_tasks.get(session_id)
            if task is None:
                task = asyncio.create_task(self._end_once(result))
                self._end_tasks[session_id] = task
                task.add_done_callback(self._consume_cleanup_result)
        await asyncio.shield(task)

    async def _end_once(self, result: BootstrapResult) -> None:
        try:
            await self._runtimes.stop(result.session_id)
            if not getattr(self._runtimes, "manages_owned_cleanup", False):
                await self._rooms.delete(result.room)
                await self._sessions.delete(result.session_id)
            async with self._lock:
                self._complete_end(result)
        finally:
            current_task = asyncio.current_task()
            async with self._lock:
                if self._end_tasks.get(result.session_id) is current_task:
                    self._end_tasks.pop(result.session_id, None)

    def _complete_end(self, result: BootstrapResult) -> None:
        self._by_session.pop(result.session_id, None)
        completed_request_ids = [
            request_id
            for request_id, (_, completed_result) in self._completed.items()
            if completed_result.session_id == result.session_id
        ]
        for request_id in completed_request_ids:
            self._completed.pop(request_id, None)

    async def _bootstrap_once(self, request: dict[str, object]) -> BootstrapResult:
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        session_id: str | None = None
        participant_id = str(uuid4())
        room_created = False
        runtime_started = False
        try:
            async with asyncio.timeout_at(deadline):
                session_id = await self._sessions.reserve(
                    {**request, "participant_id": participant_id}
                )
                room = f"voice-{session_id}"
                room_created = True
                await self._rooms.create(room)
                runtime_started = True
                await self._runtimes.connect(session_id)
                await self._runtimes.wait_until_ready(session_id)
                token = await self._issue_user_token(session_id)
                reconnect_grace_ms = min(
                    int(request["requested_reconnect_grace_ms"]),
                    MAX_RECONNECT_GRACE_MS,
                )
                return BootstrapResult(
                    session_id=session_id,
                    participant_id=participant_id,
                    room=room,
                    token=token,
                    reconnect_grace_ms=reconnect_grace_ms,
                )
        except TimeoutError as error:
            if session_id is not None:
                await self._compensate(
                    session_id,
                    deadline=deadline,
                    runtime_started=runtime_started,
                    room_created=room_created,
                )
            raise BootstrapTimeoutError("LiveKit bootstrap timed out") from error
        except BaseException as error:
            if session_id is not None:
                completed = await self._compensate(
                    session_id,
                    deadline=deadline,
                    runtime_started=runtime_started,
                    room_created=room_created,
                )
                if not completed:
                    raise BootstrapTimeoutError("LiveKit bootstrap timed out") from error
            raise

    async def _compensate(
        self,
        session_id: str,
        *,
        deadline: float,
        runtime_started: bool,
        room_created: bool,
    ) -> bool:
        session_cleanup = asyncio.create_task(self._sessions.delete(session_id))
        # session binding の解除を先に開始し、外部 I/O の停止から独立させる。
        await asyncio.sleep(0)
        cleanup_tasks = [session_cleanup]
        if runtime_started:
            cleanup_tasks.append(asyncio.create_task(self._runtimes.stop(session_id)))
        if room_created and not getattr(
            self._runtimes, "manages_owned_cleanup", False
        ):
            cleanup_tasks.append(
                asyncio.create_task(self._rooms.delete(f"voice-{session_id}"))
            )

        # 各 cleanup を少なくとも開始し、同じ絶対 deadline の残り時間だけ待つ。
        await asyncio.sleep(0)
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        done, pending = await asyncio.wait(cleanup_tasks, timeout=remaining)
        completed = not pending
        for task in done:
            try:
                task.result()
            except BaseException:
                completed = False
        for task in pending:
            task.cancel()
            task.add_done_callback(self._consume_cleanup_result)
        return completed

    @staticmethod
    def _consume_cleanup_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _issue_user_token(self, session_id: str) -> str:
        return await self._signer.issue(
            identity=f"user-{session_id}",
            room=f"voice-{session_id}",
            ttl_seconds=JOIN_TOKEN_TTL_SECONDS,
            grant={
                "room_join": True,
                "can_subscribe": True,
                "can_publish": True,
                "can_publish_data": True,
                "can_publish_sources": ["microphone"],
            },
        )
