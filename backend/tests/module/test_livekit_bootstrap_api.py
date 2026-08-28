from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlparse
from uuid import UUID

import httpx
import pytest


SUPPORTED_PROTOCOL_VERSION = "1.0"
UNSUPPORTED_PROTOCOL_VERSION = "0.0"


@dataclass
class RecordingRoomManager:
    room_creations: list[object] = field(default_factory=list)
    fail_delete: bool = False

    async def create(self, room_name: str) -> None:
        self.room_creations.append({"name": room_name})

    async def delete(self, room_name: str) -> None:
        if self.fail_delete:
            raise RuntimeError(f"failed to delete {room_name}")


@dataclass
class RecordingSessionRepository:
    delegate: object
    session_creations: list[object] = field(default_factory=list)

    async def reserve(self, request: object) -> str:
        session_id = await self.delegate.reserve(request)
        self.session_creations.append(request)
        return session_id

    async def delete(self, session_id: str) -> None:
        await self.delegate.delete(session_id)

    def get(self, session_id: str):
        return self.delegate.get(session_id)

    def contains(self, session_id: str) -> bool:
        return self.delegate.contains(session_id)


@dataclass
class RecordingRuntimeManager:
    runtime_starts: list[object] = field(default_factory=list)
    fail_ready: bool = False

    async def connect(self, session_id: str) -> None:
        self.runtime_starts.append({"session_id": session_id})

    async def wait_until_ready(self, session_id: str) -> None:
        if self.fail_ready:
            raise RuntimeError(f"runtime {session_id} is not ready")

    async def stop(self, session_id: str) -> None:
        del session_id


@dataclass
class RecordingTokenSigner:
    api_secret: str = "LIVEKIT_SECRET_SENTINEL"
    token_issues: list[object] = field(default_factory=list)

    async def issue_with_expiration(self, **request: object):
        from app.livekit_transport.token import IssuedToken

        self.token_issues.append(request)
        return IssuedToken(
            token="safe-user-token",
            expires_at=datetime(2026, 8, 27, 0, 1, 30, tzinfo=UTC),
        )


@dataclass(frozen=True)
class RecordingLiveKitResources:
    room_manager: RecordingRoomManager
    session_repository: RecordingSessionRepository
    runtime_manager: RecordingRuntimeManager
    token_signer: RecordingTokenSigner


def _install_livekit_resource_ports(client, monkeypatch) -> RecordingLiveKitResources:
    from app.livekit_transport.bootstrap import BootstrapService, InMemorySessionBindingRepository

    session_bindings = InMemorySessionBindingRepository(
        session_id_factory=lambda: "20000000-0000-4000-8000-000000000001"
    )
    resources = RecordingLiveKitResources(
        room_manager=RecordingRoomManager(),
        session_repository=RecordingSessionRepository(session_bindings),
        runtime_manager=RecordingRuntimeManager(),
        token_signer=RecordingTokenSigner(),
    )
    monkeypatch.setattr(
        client.app.state,
        "livekit_room_manager",
        resources.room_manager,
        raising=False,
    )
    monkeypatch.setattr(
        client.app.state,
        "livekit_session_repository",
        resources.session_repository,
        raising=False,
    )
    monkeypatch.setattr(
        client.app.state,
        "livekit_runtime_manager",
        resources.runtime_manager,
        raising=False,
    )
    monkeypatch.setattr(
        client.app.state,
        "livekit_token_signer",
        resources.token_signer,
        raising=False,
    )
    monkeypatch.setattr(
        client.app.state,
        "livekit_url",
        "ws://127.0.0.1:7880",
        raising=False,
    )
    service = BootstrapService(
        session_repository=resources.session_repository,
        room_manager=resources.room_manager,
        runtime_manager=resources.runtime_manager,
        token_signer=resources.token_signer,
        timeout_seconds=10,
    )
    monkeypatch.setattr(
        client.app.state, "livekit_bootstrap_service", service, raising=False
    )
    return resources


def _bootstrap_request(**overrides: object) -> dict[str, object]:
    return {
        "protocol_version": SUPPORTED_PROTOCOL_VERSION,
        "request_id": "10000000-0000-4000-8000-000000000001",
        "character_id": "miori",
        "conversation_id": "20000000-0000-4000-8000-000000000001",
        "requested_reconnect_grace_ms": 60_000,
        **overrides,
    }


def _all_scalar_values(value: object) -> list[object]:
    if isinstance(value, Mapping):
        return [
            scalar
            for child in cast(Mapping[object, object], value).values()
            for scalar in _all_scalar_values(child)
        ]
    if isinstance(value, list):
        return [scalar for child in value for scalar in _all_scalar_values(child)]
    return [value]


def test_protocol_mismatch_is_rejected_before_livekit_configuration_is_used(
    client,
    monkeypatch,
) -> None:
    resources = _install_livekit_resource_ports(client, monkeypatch)

    response = client.post(
        "/voice/livekit/token",
        json=_bootstrap_request(protocol_version=UNSUPPORTED_PROTOCOL_VERSION),
    )

    assert resources.room_manager.room_creations == []
    assert resources.session_repository.session_creations == []
    assert resources.runtime_manager.runtime_starts == []
    assert resources.token_signer.token_issues == []
    assert response.status_code == 409
    values = _all_scalar_values(response.json())
    assert "protocol_version_mismatch" in values
    assert SUPPORTED_PROTOCOL_VERSION in values


def test_missing_resolved_livekit_url_is_rejected_before_resources_are_created(
    client,
    monkeypatch,
) -> None:
    resources = _install_livekit_resource_ports(client, monkeypatch)
    monkeypatch.delattr(client.app.state, "livekit_url")

    response = client.post("/voice/livekit/token", json=_bootstrap_request())

    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "livekit_not_configured"}
    assert resources.room_manager.room_creations == []
    assert resources.session_repository.session_creations == []
    assert resources.runtime_manager.runtime_starts == []
    assert resources.token_signer.token_issues == []


@pytest.mark.parametrize(
    "conflicting_overrides",
    [
        {"requested_reconnect_grace_ms": 30_000},
        {"request_id": "10000000-0000-4000-8000-000000000002"},
    ],
    ids=("same-request-id-different-payload", "another-request"),
)
def test_bootstrap_conflict_returns_409_without_creating_more_resources(
    client,
    monkeypatch,
    conflicting_overrides: dict[str, object],
) -> None:
    resources = _install_livekit_resource_ports(client, monkeypatch)
    first = client.post("/voice/livekit/token", json=_bootstrap_request())
    resource_counts = (
        len(resources.room_manager.room_creations),
        len(resources.session_repository.session_creations),
        len(resources.runtime_manager.runtime_starts),
    )

    conflict = client.post(
        "/voice/livekit/token",
        json=_bootstrap_request(**conflicting_overrides),
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert (
        len(resources.room_manager.room_creations),
        len(resources.session_repository.session_creations),
        len(resources.runtime_manager.runtime_starts),
    ) == resource_counts


def test_token_api_response_does_not_expose_api_secret(client, monkeypatch) -> None:
    resources = _install_livekit_resource_ports(client, monkeypatch)

    response = client.post("/voice/livekit/token", json=_bootstrap_request())

    assert response.status_code == 200
    assert resources.token_signer.api_secret not in _all_scalar_values(response.json())


def test_token_api_returns_complete_bootstrap_binding(client, monkeypatch) -> None:
    _install_livekit_resource_ports(client, monkeypatch)

    response = client.post("/voice/livekit/token", json=_bootstrap_request())

    assert response.status_code == 200
    payload = response.json()
    assert {
        "session_id",
        "participant_id",
        "room",
        "token",
        "livekit_url",
        "expires_at",
        "reconnect_grace_ms",
    } <= payload.keys()
    session_id = str(UUID(payload["session_id"]))
    UUID(payload["participant_id"])
    assert payload["room"] == f"voice-{session_id}"
    assert payload["token"]
    livekit_url = urlparse(payload["livekit_url"])
    assert livekit_url.scheme in {"ws", "wss"}
    assert livekit_url.netloc
    expires_at = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    assert expires_at.utcoffset() == UTC.utcoffset(expires_at)
    assert expires_at == datetime(2026, 8, 27, 0, 1, 30, tzinfo=UTC)
    assert payload["reconnect_grace_ms"] == 60_000


def test_reconnect_grace_above_public_limit_is_rejected_before_bootstrap(
    client,
    monkeypatch,
) -> None:
    resources = _install_livekit_resource_ports(client, monkeypatch)

    response = client.post(
        "/voice/livekit/token",
        json=_bootstrap_request(requested_reconnect_grace_ms=60_001),
    )

    assert response.status_code == 422
    assert resources.session_repository.session_creations == []
    assert resources.token_signer.token_issues == []


def test_incomplete_compensation_returns_bootstrap_timeout_without_token(
    client,
    monkeypatch,
) -> None:
    resources = _install_livekit_resource_ports(client, monkeypatch)
    resources.runtime_manager.fail_ready = True
    resources.room_manager.fail_delete = True

    response = client.post("/voice/livekit/token", json=_bootstrap_request())

    assert response.status_code == 504
    assert response.json() == {"detail": {"code": "bootstrap_timeout"}}
    assert "token" not in response.json()
    assert resources.token_signer.token_issues == []


def test_existing_health_endpoint_remains_available(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_repeated_session_end_returns_the_same_ended_state(client, monkeypatch) -> None:
    session_id = "20000000-0000-4000-8000-000000000001"
    _install_livekit_resource_ports(client, monkeypatch)

    first = client.delete(f"/voice/livekit/sessions/{session_id}")
    second = client.delete(f"/voice/livekit/sessions/{session_id}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["session_id"] == session_id
    assert first.json()["phase"] == "ended"


def test_session_end_retries_failed_room_cleanup_before_returning_ended(
    client,
    monkeypatch,
) -> None:
    from app.livekit_transport.bootstrap import (
        BootstrapService,
        InMemorySessionBindingRepository,
    )
    from app.livekit_transport.coordinator import (
        ProductionSessionCoordinator,
        SessionCoordinatorDependencies,
    )
    from app.livekit_transport.production import ProductionRuntimeManager

    session_id = "20000000-0000-4000-8000-000000000001"
    room_name = f"voice-{session_id}"
    disconnect_calls: list[str] = []
    coordinator_cleanup_calls: list[str] = []

    class RetryRoomManager(RecordingRoomManager):
        delete_calls = 0
        active_rooms = {room_name}

        async def delete(self, deleted_room_name: str) -> None:
            assert deleted_room_name == room_name
            self.delete_calls += 1
            if self.delete_calls == 1:
                raise RuntimeError("room deletion failed")
            self.active_rooms.remove(deleted_room_name)

    class ConnectedRoom:
        async def disconnect(self) -> None:
            disconnect_calls.append(session_id)

    class CorePort:
        def notify(self, payload: bytes) -> None:
            del payload

    sessions = InMemorySessionBindingRepository(session_id_factory=lambda: session_id)
    recording_sessions = RecordingSessionRepository(sessions)
    rooms = RetryRoomManager()
    signer = RecordingTokenSigner()
    runtime = ProductionRuntimeManager(
        livekit_url="ws://127.0.0.1:7880",
        signer=signer,
        room_manager=rooms,
        session_repository=recording_sessions,
        core_port=CorePort(),
    )

    async def connect(owned_session_id: str) -> None:
        async def publish_data(payload: bytes, topic: str) -> None:
            del payload, topic

        async def cleanup(cleanup_session_id: str) -> None:
            await runtime._cleanup_owned_session(cleanup_session_id)

        async def generation_ready() -> None:
            return None

        class RecordingCoordinator(ProductionSessionCoordinator):
            async def cleanup(self, reason: str) -> None:
                coordinator_cleanup_calls.append(reason)
                await super().cleanup(reason)

        coordinator = RecordingCoordinator(
            session_id=owned_session_id,
            user_identity=f"user-{owned_session_id}",
            core_participant_id=owned_session_id,
            reconnect_grace_ms=60_000,
            dependencies=SessionCoordinatorDependencies(
                publish_data=publish_data,
                cleanup=cleanup,
                generation_ready=generation_ready,
            ),
            core_port=CorePort(),
        )
        runtime._rooms[owned_session_id] = ConnectedRoom()
        runtime._coordinators[owned_session_id] = coordinator
        runtime._session_tasks[owned_session_id] = set()
        runtime._ready[owned_session_id] = asyncio.Event()
        runtime._ready[owned_session_id].set()

    runtime.connect = connect
    service = BootstrapService(
        session_repository=recording_sessions,
        room_manager=rooms,
        runtime_manager=runtime,
        token_signer=signer,
        timeout_seconds=10,
    )
    monkeypatch.setattr(client.app.state, "livekit_room_manager", rooms, raising=False)
    monkeypatch.setattr(
        client.app.state, "livekit_session_repository", recording_sessions, raising=False
    )
    monkeypatch.setattr(
        client.app.state, "livekit_runtime_manager", runtime, raising=False
    )
    monkeypatch.setattr(client.app.state, "livekit_token_signer", signer, raising=False)
    monkeypatch.setattr(
        client.app.state, "livekit_url", "ws://127.0.0.1:7880", raising=False
    )
    monkeypatch.setattr(
        client.app.state, "livekit_bootstrap_service", service, raising=False
    )

    async def exercise_room_cleanup_retry():
        transport = httpx.ASGITransport(
            app=client.app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as async_client:
            created = await async_client.post(
                "/voice/livekit/token", json=_bootstrap_request()
            )
            first = await async_client.delete(
                f"/voice/livekit/sessions/{session_id}"
            )
            room_after_failure = set(rooms.active_rooms)
            second = await async_client.delete(
                f"/voice/livekit/sessions/{session_id}"
            )
            third = await async_client.delete(
                f"/voice/livekit/sessions/{session_id}"
            )
        return (
            created,
            first,
            room_after_failure,
            second,
            third,
        )

    created, first, room_after_failure, second, third = (
        asyncio.run(exercise_room_cleanup_retry())
    )

    assert created.status_code == 200
    assert first.status_code == 500
    assert room_after_failure == {room_name}
    assert second.status_code == 200
    assert third.status_code == 200
    assert second.json() == third.json() == {
        "session_id": session_id,
        "phase": "ended",
    }
    assert coordinator_cleanup_calls == ["explicit"]
    assert disconnect_calls == [session_id]
    assert recording_sessions.contains(session_id) is False
    assert session_id not in runtime._rooms
    assert rooms.delete_calls == 2
    assert rooms.active_rooms == set()


def test_concurrent_session_end_shares_local_cleanup_result_and_allows_retry(
    client,
    monkeypatch,
) -> None:
    from app.livekit_transport.bootstrap import (
        BootstrapService,
        InMemorySessionBindingRepository,
    )
    from app.livekit_transport.production import ProductionRuntimeManager

    session_id = "20000000-0000-4000-8000-000000000001"
    room_name = f"voice-{session_id}"
    disconnect_calls: list[str] = []
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    class RetrySessionRepository(RecordingSessionRepository):
        delete_attempts = 0

        async def delete(self, deleted_session_id: str) -> None:
            assert deleted_session_id == session_id
            self.delete_attempts += 1
            if self.delete_attempts == 1:
                cleanup_started.set()
                await cleanup_release.wait()
                raise RuntimeError("session binding deletion failed")
            await super().delete(deleted_session_id)

    class CorePort:
        def notify(self, payload: bytes) -> None:
            del payload

    class ConnectedRoom:
        async def disconnect(self) -> None:
            disconnect_calls.append(session_id)

    class RecordingDeleteRoomManager(RecordingRoomManager):
        delete_calls: list[str] = []

        async def delete(self, deleted_room_name: str) -> None:
            self.delete_calls.append(deleted_room_name)

    sessions = InMemorySessionBindingRepository(session_id_factory=lambda: session_id)
    recording_sessions = RetrySessionRepository(sessions)
    rooms = RecordingDeleteRoomManager()
    signer = RecordingTokenSigner()
    runtime = ProductionRuntimeManager(
        livekit_url="ws://127.0.0.1:7880",
        signer=signer,
        room_manager=rooms,
        session_repository=recording_sessions,
        core_port=CorePort(),
    )

    async def connect(owned_session_id: str) -> None:
        runtime._rooms[owned_session_id] = ConnectedRoom()
        runtime._session_tasks[owned_session_id] = set()
        runtime._ready[owned_session_id] = asyncio.Event()
        runtime._ready[owned_session_id].set()

    runtime.connect = connect
    service = BootstrapService(
        session_repository=recording_sessions,
        room_manager=rooms,
        runtime_manager=runtime,
        token_signer=signer,
        timeout_seconds=10,
    )
    monkeypatch.setattr(
        client.app.state, "livekit_bootstrap_service", service, raising=False
    )
    monkeypatch.setattr(
        client.app.state, "livekit_url", "ws://127.0.0.1:7880", raising=False
    )

    async def exercise_concurrent_end_requests():
        end_arrivals = 0
        second_end_arrived = asyncio.Event()
        original_end = service.end

        async def observed_end(ended_session_id: str) -> None:
            nonlocal end_arrivals
            end_arrivals += 1
            if end_arrivals == 2:
                second_end_arrived.set()
            await original_end(ended_session_id)

        service.end = observed_end
        transport = httpx.ASGITransport(
            app=client.app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as async_client:
            created = await async_client.post(
                "/voice/livekit/token", json=_bootstrap_request()
            )
            first_request = asyncio.create_task(
                async_client.delete(f"/voice/livekit/sessions/{session_id}")
            )
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            second_request = asyncio.create_task(
                async_client.delete(f"/voice/livekit/sessions/{session_id}")
            )
            await asyncio.wait_for(second_end_arrived.wait(), timeout=1)
            cleanup_release.set()
            first, second = await asyncio.wait_for(
                asyncio.gather(first_request, second_request),
                timeout=1,
            )
            binding_after_shared_failure = recording_sessions.contains(session_id)
            third = await async_client.delete(
                f"/voice/livekit/sessions/{session_id}"
            )
            fourth = await async_client.delete(
                f"/voice/livekit/sessions/{session_id}"
            )
        return created, first, second, binding_after_shared_failure, third, fourth

    created, first, second, binding_after_failure, third, fourth = asyncio.run(
        exercise_concurrent_end_requests()
    )

    assert created.status_code == 200
    assert first.status_code == 500
    assert second.status_code == 500
    assert binding_after_failure is True
    assert third.status_code == 200
    assert fourth.status_code == 200
    assert third.json() == fourth.json() == {
        "session_id": session_id,
        "phase": "ended",
    }
    assert recording_sessions.delete_attempts == 2
    assert recording_sessions.contains(session_id) is False
    assert rooms.delete_calls == [room_name]
    assert disconnect_calls == [session_id]


def test_cancelled_session_end_waiter_does_not_cancel_shared_cleanup(
    client,
    monkeypatch,
) -> None:
    from app.livekit_transport.bootstrap import (
        BootstrapService,
        InMemorySessionBindingRepository,
    )

    session_id = "20000000-0000-4000-8000-000000000001"
    room_name = f"voice-{session_id}"
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    class RecordingDeleteSessionRepository(RecordingSessionRepository):
        delete_calls: list[str] = []

        async def delete(self, deleted_session_id: str) -> None:
            self.delete_calls.append(deleted_session_id)
            await super().delete(deleted_session_id)

    class RecordingDeleteRoomManager(RecordingRoomManager):
        delete_calls: list[str] = []

        async def delete(self, deleted_room_name: str) -> None:
            self.delete_calls.append(deleted_room_name)

    class BlockingRuntimeManager(RecordingRuntimeManager):
        stop_calls: list[str] = []

        async def stop(self, stopped_session_id: str) -> None:
            self.stop_calls.append(stopped_session_id)
            cleanup_started.set()
            await cleanup_release.wait()

    sessions = InMemorySessionBindingRepository(session_id_factory=lambda: session_id)
    recording_sessions = RecordingDeleteSessionRepository(sessions)
    rooms = RecordingDeleteRoomManager()
    runtime = BlockingRuntimeManager()
    service = BootstrapService(
        session_repository=recording_sessions,
        room_manager=rooms,
        runtime_manager=runtime,
        token_signer=RecordingTokenSigner(),
        timeout_seconds=10,
    )
    monkeypatch.setattr(
        client.app.state, "livekit_bootstrap_service", service, raising=False
    )
    monkeypatch.setattr(
        client.app.state, "livekit_url", "ws://127.0.0.1:7880", raising=False
    )

    async def exercise_cancelled_end_waiter():
        end_arrivals = 0
        second_end_arrived = asyncio.Event()
        original_end = service.end

        async def observed_end(ended_session_id: str) -> None:
            nonlocal end_arrivals
            end_arrivals += 1
            if end_arrivals == 2:
                second_end_arrived.set()
            await original_end(ended_session_id)

        service.end = observed_end
        transport = httpx.ASGITransport(
            app=client.app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as async_client:
            created = await async_client.post(
                "/voice/livekit/token", json=_bootstrap_request()
            )
            first_request = asyncio.create_task(
                async_client.delete(f"/voice/livekit/sessions/{session_id}")
            )
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            first_request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_request

            second_request = asyncio.create_task(
                async_client.delete(f"/voice/livekit/sessions/{session_id}")
            )
            await asyncio.wait_for(second_end_arrived.wait(), timeout=1)
            cleanup_release.set()
            second = await asyncio.wait_for(second_request, timeout=1)
            third = await async_client.delete(
                f"/voice/livekit/sessions/{session_id}"
            )
        return created, second, third

    created, second, third = asyncio.run(exercise_cancelled_end_waiter())

    assert created.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert second.json() == third.json() == {
        "session_id": session_id,
        "phase": "ended",
    }
    assert runtime.stop_calls == [session_id]
    assert rooms.delete_calls == [room_name]
    assert recording_sessions.delete_calls == [session_id]
    assert recording_sessions.contains(session_id) is False
