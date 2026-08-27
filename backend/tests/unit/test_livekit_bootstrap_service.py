from __future__ import annotations

import asyncio
import importlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast
from datetime import UTC, datetime

import pytest

from app.conversation_history.errors import (
    ConversationCharacterBoundaryError,
    ConversationNotFoundError,
)


def _bootstrap_module(contract: str):
    module_name = "app.livekit_transport.bootstrap"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is None or not (
            error.name == module_name or module_name.startswith(f"{error.name}.")
        ):
            raise
    pytest.fail(f"{module_name} must implement {contract}")


@dataclass
class FakeSessionRepository:
    calls: list[str]
    active: set[str] = field(default_factory=set)
    hang_reserve: bool = False

    async def reserve(self, request: object) -> str:
        self.calls.append("session.reserve")
        if self.hang_reserve:
            await asyncio.Event().wait()
        self.active.add("20000000-0000-4000-8000-000000000010")
        return "20000000-0000-4000-8000-000000000010"

    async def delete(self, session_id: str) -> None:
        self.calls.append("session.delete")
        self.active.discard(session_id)


@dataclass
class FakeRoomManager:
    calls: list[str]
    rooms: set[str] = field(default_factory=set)
    hang_create: bool = False
    hang_delete: bool = False
    fail_delete: bool = False

    async def create(self, room_name: str) -> None:
        self.calls.append("room.create")
        self.rooms.add(room_name)
        if self.hang_create:
            await asyncio.Event().wait()

    async def delete(self, room_name: str) -> None:
        self.calls.append("room.delete")
        if self.hang_delete:
            await asyncio.Event().wait()
        if self.fail_delete:
            raise RuntimeError("room deletion failed")
        self.rooms.discard(room_name)


@dataclass
class FakeRuntimeManager:
    calls: list[str]
    manages_owned_cleanup: bool = False
    fail_ready: bool = False
    ready_error: Exception | None = None
    hang_connect: bool = False
    hang_ready: bool = False
    hang_stop: bool = False
    active: set[str] = field(default_factory=set)
    ready_started: asyncio.Event | None = None
    ready_release: asyncio.Event | None = None

    async def connect(self, session_id: str) -> None:
        self.calls.append("runtime.connect")
        self.active.add(session_id)
        if self.hang_connect:
            await asyncio.Event().wait()

    async def wait_until_ready(self, session_id: str) -> None:
        self.calls.append("runtime.ready")
        if self.ready_error is not None:
            raise self.ready_error
        if self.fail_ready:
            raise TimeoutError("control/audio readiness timeout")
        if self.hang_ready:
            await asyncio.Event().wait()
        if self.ready_started is not None and self.ready_release is not None:
            self.ready_started.set()
            await self.ready_release.wait()

    async def stop(self, session_id: str) -> None:
        self.calls.append("runtime.stop")
        self.active.discard(session_id)
        if self.hang_stop:
            await asyncio.Event().wait()


@dataclass
class FakeTokenSigner:
    calls: list[str]
    api_secret: str = "LIVEKIT_SECRET_SENTINEL"
    grants: list[dict[str, object]] = field(default_factory=list)
    issued_tokens: list[dict[str, object]] = field(default_factory=list)
    hang_issue: bool = False

    async def issue_with_expiration(
        self,
        *,
        identity: str,
        room: str,
        ttl_seconds: int,
        grant: dict[str, object],
    ):
        from app.livekit_transport.token import IssuedToken

        self.calls.append("token.issue")
        if self.hang_issue:
            await asyncio.Event().wait()
        self.grants.append(grant)
        self.issued_tokens.append(
            {
                "identity": identity,
                "room": room,
                "ttl_seconds": ttl_seconds,
                "grant": grant,
            }
        )
        return IssuedToken(
            token="safe-user-token",
            expires_at=datetime(2026, 8, 27, 0, 1, 30, tzinfo=UTC),
        )


def _request() -> dict[str, object]:
    return {
        "protocol_version": "1.0",
        "request_id": "10000000-0000-4000-8000-000000000010",
        "character_id": "miori",
        "conversation_id": "20000000-0000-4000-8000-000000000011",
        "requested_reconnect_grace_ms": 60_000,
    }


def _scalar_values(value: object) -> list[object]:
    if isinstance(value, Mapping):
        return [
            scalar
            for child in cast(Mapping[object, object], value).values()
            for scalar in _scalar_values(child)
        ]
    if isinstance(value, (list, tuple)):
        return [scalar for child in value for scalar in _scalar_values(child)]
    return [value]


def _service(module, *, fail_ready: bool = False):
    calls: list[str] = []
    sessions = FakeSessionRepository(calls)
    rooms = FakeRoomManager(calls)
    runtimes = FakeRuntimeManager(calls, fail_ready=fail_ready)
    signer = FakeTokenSigner(calls)
    service = module.BootstrapService(
        session_repository=sessions,
        room_manager=rooms,
        runtime_manager=runtimes,
        token_signer=signer,
        timeout_seconds=10,
    )
    return service, calls, sessions, rooms, runtimes, signer


def test_bootstrap_issues_user_token_only_after_runtime_is_ready() -> None:
    module = _bootstrap_module("ordered bootstrap and least-privilege token issuance")
    service, calls, _, rooms, runtimes, signer = _service(module)

    result = asyncio.run(service.bootstrap(_request()))

    assert calls == [
        "session.reserve",
        "room.create",
        "runtime.connect",
        "runtime.ready",
        "token.issue",
    ]
    assert result.session_id == "20000000-0000-4000-8000-000000000010"
    assert result.token == "safe-user-token"
    assert rooms.rooms == {"voice-20000000-0000-4000-8000-000000000010"}
    assert runtimes.active == {"20000000-0000-4000-8000-000000000010"}
    assert signer.grants == [
        {
            "room_join": True,
            "can_subscribe": True,
            "can_publish": True,
            "can_publish_data": True,
            "can_publish_sources": ["microphone"],
        }
    ]
    assert signer.issued_tokens == [
        {
            "identity": "user-20000000-0000-4000-8000-000000000010",
            "room": "voice-20000000-0000-4000-8000-000000000010",
            "ttl_seconds": 90,
            "grant": {
                "room_join": True,
                "can_subscribe": True,
                "can_publish": True,
                "can_publish_data": True,
                "can_publish_sources": ["microphone"],
            },
        }
    ]
    assert "room_create" not in signer.issued_tokens[0]["grant"]
    result_fields = (
        result.model_dump() if hasattr(result, "model_dump") else vars(result)
    )
    assert signer.api_secret not in _scalar_values(result_fields)
    assert module.BOOTSTRAP_TIMEOUT_SECONDS == 10
    assert module.JOIN_TOKEN_TTL_SECONDS == 90


def test_bootstrap_caps_requested_reconnect_grace_at_sixty_seconds() -> None:
    module = _bootstrap_module("reconnect grace upper bound")
    service, _, _, _, _, _ = _service(module)
    request = {**_request(), "requested_reconnect_grace_ms": 60_001}

    result = asyncio.run(service.bootstrap(request))

    assert result.reconnect_grace_ms == 60_000


def test_readiness_timeout_returns_no_token_and_compensates_resources() -> None:
    module = _bootstrap_module("timeout compensation before token issuance")
    service, calls, sessions, rooms, runtimes, signer = _service(
        module,
        fail_ready=True,
    )

    with pytest.raises(module.BootstrapTimeoutError):
        asyncio.run(service.bootstrap(_request()))

    assert calls[:4] == [
        "session.reserve",
        "room.create",
        "runtime.connect",
        "runtime.ready",
    ]
    assert calls.index("session.delete") < calls.index("runtime.stop")
    assert {"session.delete", "runtime.stop", "room.delete"}.issubset(calls)
    assert signer.grants == []
    assert sessions.active == set()
    assert rooms.rooms == set()
    assert runtimes.active == set()


def test_cleanup_exception_is_not_treated_as_success() -> None:
    module = _bootstrap_module("cleanup result aggregation")
    calls: list[str] = []
    sessions = FakeSessionRepository(calls)
    rooms = FakeRoomManager(calls, fail_delete=True)
    runtimes = FakeRuntimeManager(
        calls,
        ready_error=RuntimeError("readiness failed"),
    )
    signer = FakeTokenSigner(calls)
    service = module.BootstrapService(
        session_repository=sessions,
        room_manager=rooms,
        runtime_manager=runtimes,
        token_signer=signer,
        timeout_seconds=10,
    )

    with pytest.raises(module.BootstrapTimeoutError, match="bootstrap timed out"):
        asyncio.run(service.bootstrap(_request()))

    assert signer.grants == []
    assert sessions.active == set()
    assert runtimes.active == set()
    assert rooms.rooms == {"voice-20000000-0000-4000-8000-000000000010"}


@pytest.mark.parametrize("blocked_cleanup", ["runtime", "room"])
def test_bootstrap_cleanup_uses_an_independent_deadline_and_releases_all_ownership(
    blocked_cleanup: str,
) -> None:
    module = _bootstrap_module("independent compensation deadline")
    calls: list[str] = []
    sessions = FakeSessionRepository(calls)
    rooms = FakeRoomManager(calls, hang_delete=blocked_cleanup == "room")
    runtimes = FakeRuntimeManager(
        calls,
        fail_ready=True,
        hang_stop=blocked_cleanup == "runtime",
    )
    signer = FakeTokenSigner(calls)
    service = module.BootstrapService(
        session_repository=sessions,
        room_manager=rooms,
        runtime_manager=runtimes,
        token_signer=signer,
        timeout_seconds=0.01,
    )

    with pytest.raises(module.BootstrapTimeoutError, match="bootstrap timed out"):
            asyncio.run(asyncio.wait_for(service.bootstrap(_request()), timeout=1.5))

    assert "session.delete" in calls
    assert "runtime.stop" in calls
    assert "room.delete" in calls
    assert signer.grants == []
    assert sessions.active == set()
    expected_rooms = (
        {"voice-20000000-0000-4000-8000-000000000010"}
        if blocked_cleanup == "room"
        else set()
    )
    assert rooms.rooms == expected_rooms
    assert runtimes.active == set()


@pytest.mark.parametrize(
    "stage", ["session", "room", "runtime", "readiness", "token"]
)
def test_bootstrap_timeout_covers_every_resource_acquisition_stage(stage: str) -> None:
    module = _bootstrap_module("whole-bootstrap timeout and compensation")
    calls: list[str] = []
    sessions = FakeSessionRepository(calls, hang_reserve=stage == "session")
    rooms = FakeRoomManager(calls, hang_create=stage == "room")
    runtimes = FakeRuntimeManager(
        calls,
        hang_connect=stage == "runtime",
        hang_ready=stage == "readiness",
    )
    signer = FakeTokenSigner(calls, hang_issue=stage == "token")
    service = module.BootstrapService(
        session_repository=sessions,
        room_manager=rooms,
        runtime_manager=runtimes,
        token_signer=signer,
        timeout_seconds=0.01,
    )

    with pytest.raises(module.BootstrapTimeoutError, match="bootstrap timed out"):
        asyncio.run(asyncio.wait_for(service.bootstrap(_request()), timeout=0.5))

    if stage == "token":
        assert calls[:5] == [
            "session.reserve",
            "room.create",
            "runtime.connect",
            "runtime.ready",
            "token.issue",
        ]
        assert calls.index("session.delete") < calls.index("runtime.stop")
        assert {"session.delete", "runtime.stop", "room.delete"}.issubset(calls)
    assert signer.issued_tokens == []
    assert sessions.active == set()
    assert rooms.rooms == set()
    assert runtimes.active == set()


def test_same_request_retry_reuses_session_without_new_room_or_runtime() -> None:
    module = _bootstrap_module("idempotent retry for the same request")
    service, calls, _, _, _, _ = _service(module)

    first = asyncio.run(service.bootstrap(_request()))
    second = asyncio.run(service.bootstrap(_request()))

    assert first.session_id == second.session_id
    assert calls.count("room.create") == 1
    assert calls.count("runtime.connect") == 1
    assert calls.count("token.issue") == 2


def test_same_request_id_with_different_payload_is_rejected_before_resources() -> None:
    module = _bootstrap_module("exclusive bootstrap per conversation")
    calls: list[str] = []
    repository = module.InMemorySessionBindingRepository(
        session_id_factory=lambda: "20000000-0000-4000-8000-000000000010"
    )
    rooms = FakeRoomManager(calls)
    runtimes = FakeRuntimeManager(calls)
    signer = FakeTokenSigner(calls)
    service = module.BootstrapService(
        session_repository=repository,
        room_manager=rooms,
        runtime_manager=runtimes,
        token_signer=signer,
        timeout_seconds=10,
    )
    asyncio.run(service.bootstrap(_request()))
    conflicting_request = {**_request(), "requested_reconnect_grace_ms": 30_000}

    with pytest.raises(module.BootstrapConflictError) as raised:
        asyncio.run(service.bootstrap(conflicting_request))

    assert raised.value.status_code == 409
    assert calls.count("room.create") == 1
    assert calls.count("runtime.connect") == 1
    assert calls.count("token.issue") == 1


def test_parallel_request_for_same_conversation_creates_no_resources() -> None:
    module = _bootstrap_module("parallel bootstrap exclusion before resource creation")
    async def run_parallel_bootstrap() -> tuple[object, BaseException, list[str]]:
        calls: list[str] = []
        session_ids = iter(
            (
                "20000000-0000-4000-8000-000000000010",
                "20000000-0000-4000-8000-000000000012",
            )
        )
        repository = module.InMemorySessionBindingRepository(
            session_id_factory=lambda: next(session_ids)
        )
        ready_started = asyncio.Event()
        ready_release = asyncio.Event()
        service = module.BootstrapService(
            session_repository=repository,
            room_manager=FakeRoomManager(calls),
            runtime_manager=FakeRuntimeManager(
                calls,
                ready_started=ready_started,
                ready_release=ready_release,
            ),
            token_signer=FakeTokenSigner(calls),
            timeout_seconds=10,
        )
        first_task = asyncio.create_task(service.bootstrap(_request()))
        await ready_started.wait()
        another_request = {
            **_request(),
            "request_id": "10000000-0000-4000-8000-000000000011",
        }
        try:
            await service.bootstrap(another_request)
        except Exception as error:
            conflict = error
        else:
            raise AssertionError("parallel bootstrap must be rejected")
        ready_release.set()
        first = await first_task
        return first, conflict, calls

    first, conflict, calls = asyncio.run(run_parallel_bootstrap())

    assert first.session_id == "20000000-0000-4000-8000-000000000010"
    assert isinstance(conflict, module.BootstrapConflictError)
    assert conflict.status_code == 409
    assert calls.count("room.create") == 1
    assert calls.count("runtime.connect") == 1
    assert calls.count("token.issue") == 1


@dataclass
class FakeMonotonicClock:
    value_ms: int

    def now_ms(self) -> int:
        return self.value_ms


def test_backend_restart_rejects_old_session_and_issues_a_new_session() -> None:
    module = _bootstrap_module("process-local sessions without restart restoration")
    old_calls: list[str] = []
    old_repository = module.InMemorySessionBindingRepository(
        session_id_factory=lambda: "20000000-0000-4000-8000-000000000010",
        monotonic_clock=FakeMonotonicClock(1_000),
    )
    old_service = module.BootstrapService(
        session_repository=old_repository,
        room_manager=FakeRoomManager(old_calls),
        runtime_manager=FakeRuntimeManager(old_calls),
        token_signer=FakeTokenSigner(old_calls),
        timeout_seconds=10,
    )
    old = asyncio.run(old_service.bootstrap(_request()))

    restarted_calls: list[str] = []
    restarted_repository = module.InMemorySessionBindingRepository(
        session_id_factory=lambda: "20000000-0000-4000-8000-000000000020",
        monotonic_clock=FakeMonotonicClock(2_000),
    )
    restarted_service = module.BootstrapService(
        session_repository=restarted_repository,
        room_manager=FakeRoomManager(restarted_calls),
        runtime_manager=FakeRuntimeManager(restarted_calls),
        token_signer=FakeTokenSigner(restarted_calls),
        timeout_seconds=10,
    )
    reconnect_request = {**_request(), "session_id": old.session_id}

    with pytest.raises(module.UnknownSessionError):
        asyncio.run(restarted_service.bootstrap(reconnect_request))

    assert restarted_calls == []

    fresh_request = {
        **_request(),
        "request_id": "10000000-0000-4000-8000-000000000020",
    }
    fresh = asyncio.run(restarted_service.bootstrap(fresh_request))
    assert fresh.session_id == "20000000-0000-4000-8000-000000000020"
    assert fresh.session_id != old.session_id


@pytest.mark.parametrize(
    "code",
    ["character_not_found", "conversation_not_found", "session_binding_mismatch"],
)
def test_invalid_binding_is_rejected_before_any_resource_is_created(code: str) -> None:
    module = _bootstrap_module("binding validation before resource ownership")
    calls: list[str] = []

    class RejectingBindingValidator:
        def validate(self, *, character_id: str, conversation_id: object) -> None:
            calls.append(f"binding.validate:{character_id}:{conversation_id}")
            raise module.BindingValidationError(code)

    service = module.BootstrapService(
        session_repository=FakeSessionRepository(calls),
        room_manager=FakeRoomManager(calls),
        runtime_manager=FakeRuntimeManager(calls),
        token_signer=FakeTokenSigner(calls),
        timeout_seconds=10,
        binding_validator=RejectingBindingValidator(),
    )

    with pytest.raises(module.BindingValidationError) as raised:
        asyncio.run(service.bootstrap(_request()))

    assert raised.value.code == code
    assert calls == [
        "binding.validate:miori:20000000-0000-4000-8000-000000000011"
    ]


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("character_missing", "character_not_found"),
        ("conversation_missing", "conversation_not_found"),
        ("different_character", "session_binding_mismatch"),
        ("archived", "conversation_not_active"),
    ],
)
def test_real_binding_validator_rejects_before_resource_creation(
    case: str,
    expected_code: str,
) -> None:
    module = _bootstrap_module("production binding validation before resource ownership")
    calls: list[str] = []

    def load_character(character_id: str) -> object:
        calls.append(f"character.load:{character_id}")
        if case == "character_missing":
            raise FileNotFoundError(character_id)
        return object()

    class Conversations:
        def resume_conversation(self, character_id: str, conversation_id: object) -> object:
            calls.append(f"conversation.resume:{character_id}:{conversation_id}")
            if case == "conversation_missing":
                raise ConversationNotFoundError()
            if case == "different_character":
                raise ConversationCharacterBoundaryError()
            return SimpleNamespace(
                character_id=character_id,
                archived_at=object() if case == "archived" else None,
            )

    validator = module.CharacterConversationBindingValidator(
        character_loader=load_character,
        conversations=Conversations(),
    )
    service = module.BootstrapService(
        session_repository=FakeSessionRepository(calls),
        room_manager=FakeRoomManager(calls),
        runtime_manager=FakeRuntimeManager(calls),
        token_signer=FakeTokenSigner(calls),
        timeout_seconds=10,
        binding_validator=validator,
    )

    with pytest.raises(module.BindingValidationError) as raised:
        asyncio.run(service.bootstrap(_request()))

    assert raised.value.code == expected_code
    assert not any(
        call in {"session.reserve", "room.create", "runtime.connect", "token.issue"}
        for call in calls
    )


def test_real_binding_validator_allows_active_owned_conversation() -> None:
    module = _bootstrap_module("production binding validation before resource ownership")
    calls: list[str] = []

    class Conversations:
        def resume_conversation(self, character_id: str, conversation_id: object) -> object:
            calls.append(f"conversation.resume:{character_id}:{conversation_id}")
            return SimpleNamespace(character_id=character_id, archived_at=None)

    validator = module.CharacterConversationBindingValidator(
        character_loader=lambda character_id: calls.append(
            f"character.load:{character_id}"
        ),
        conversations=Conversations(),
    )
    service = module.BootstrapService(
        session_repository=FakeSessionRepository(calls),
        room_manager=FakeRoomManager(calls),
        runtime_manager=FakeRuntimeManager(calls),
        token_signer=FakeTokenSigner(calls),
        timeout_seconds=10,
        binding_validator=validator,
    )

    result = asyncio.run(service.bootstrap(_request()))

    assert result.session_id == "20000000-0000-4000-8000-000000000010"
    assert calls[:3] == [
        "character.load:miori",
        "conversation.resume:miori:20000000-0000-4000-8000-000000000011",
        "session.reserve",
    ]
