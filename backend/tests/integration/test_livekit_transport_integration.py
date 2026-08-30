from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from uuid import uuid4

import httpx
import pytest


CHARACTER_ID = "miori"
SHORT_RECONNECT_GRACE_MS = 250
STATE_WAIT_TIMEOUT_SECONDS = 10.0
APPLICATION_TOPIC = "digital-souls.core.v1"
PRIVATE_TOPIC = "digital-souls.livekit-transport.v1"


def _required_setting(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is required for the opt-in LiveKit integration suite"
    return value


def _livekit_sdk():
    try:
        from livekit import api, rtc
    except ModuleNotFoundError as error:
        if error.name not in {"livekit", "livekit.api", "livekit.rtc"}:
            raise
        pytest.fail(
            "livekit-api and livekit-rtc are required for the opt-in "
            "LiveKit integration suite"
        )
    return api, rtc


def _create_conversation(client: httpx.Client) -> str:
    response = client.post(f"/characters/{CHARACTER_ID}/conversations")
    assert response.status_code == 201
    return response.json()["conversation_id"]


def _bootstrap(
    client: httpx.Client,
    *,
    conversation_id: str,
    reconnect_grace_ms: int,
    session_id: str | None = None,
) -> httpx.Response:
    body: dict[str, object] = {
        "protocol_version": "1.0",
        "request_id": str(uuid4()),
        "character_id": CHARACTER_ID,
        "conversation_id": conversation_id,
        "requested_reconnect_grace_ms": reconnect_grace_ms,
    }
    if session_id is not None:
        body["session_id"] = session_id
    return client.post("/voice/livekit/token", json=body)


def _core_event(session_id: str, event_id: str) -> bytes:
    return json.dumps(
        {
            "protocol_version": "1.0",
            "event_id": event_id,
            "type": "session_started",
            "session_id": session_id,
            "monotonic_timestamp_ms": 1_000,
            "reconnect_grace_ms": 60_000,
        },
        separators=(",", ":"),
    ).encode()


def _send_core_from_test_app(client, session_id: str, payload: bytes) -> None:
    portal = client.portal
    assert portal is not None
    portal.call(
        client.app.state.livekit_runtime_manager.send_core,
        session_id,
        payload,
    )


async def _wait_until_test_app_session_is_available(runtime, session_id: str) -> None:
    deadline = asyncio.get_running_loop().time() + STATE_WAIT_TIMEOUT_SECONDS
    while True:
        coordinator = runtime._coordinators.get(session_id)
        if coordinator is not None and coordinator.phase == "available":
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("test app LiveKit session did not become available")
        await asyncio.sleep(0.05)


async def _cleanup_resources(
    client,
    *,
    rooms: tuple[object, ...],
    session_ids: tuple[str | None, ...],
    conversation_id: str,
) -> None:
    failures: list[BaseException] = []
    for room in rooms:
        try:
            await room.disconnect()
        except BaseException as error:
            failures.append(error)
    for session_id in session_ids:
        if session_id is None:
            continue
        try:
            client.delete(f"/voice/livekit/sessions/{session_id}").raise_for_status()
        except BaseException as error:
            failures.append(error)
    try:
        client.delete(
            f"/characters/{CHARACTER_ID}/conversations/{conversation_id}"
        ).raise_for_status()
    except BaseException as error:
        failures.append(error)
    if failures:
        raise failures[0]


async def _wait_for_participants(
    livekit_api,
    api_module,
    *,
    room_name: str,
    expected_identities: set[str],
) -> set[str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STATE_WAIT_TIMEOUT_SECONDS
    while True:
        response = await livekit_api.room.list_participants(
            api_module.ListParticipantsRequest(room=room_name)
        )
        identities = {participant.identity for participant in response.participants}
        if identities == expected_identities:
            return identities
        if loop.time() >= deadline:
            return identities
        await asyncio.sleep(0.05)


async def _wait_for_room_cleanup(
    livekit_api,
    api_module,
    *,
    room_name: str,
) -> list[str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STATE_WAIT_TIMEOUT_SECONDS
    while True:
        response = await livekit_api.room.list_rooms(
            api_module.ListRoomsRequest(names=[room_name])
        )
        room_names = [room.name for room in response.rooms]
        if room_names == []:
            return room_names
        if loop.time() >= deadline:
            return room_names
        await asyncio.sleep(0.05)


async def _connect_test_app_user(client, rtc_module):
    conversation_id = _create_conversation(client)
    response = _bootstrap(
        client,
        conversation_id=conversation_id,
        reconnect_grace_ms=60_000,
    )
    assert response.status_code == 200
    binding = response.json()
    room = rtc_module.Room()
    application_payloads: asyncio.Queue[bytes] = asyncio.Queue()

    @room.on("data_received")
    def data_received(packet) -> None:
        if packet.topic == APPLICATION_TOPIC:
            application_payloads.put_nowait(bytes(packet.data))

    await room.connect(binding["livekit_url"], binding["token"])
    portal = client.portal
    assert portal is not None
    portal.call(
        _wait_until_test_app_session_is_available,
        client.app.state.livekit_runtime_manager,
        binding["session_id"],
    )
    return conversation_id, binding, room, application_payloads


async def _acknowledge_application_event(room, event_id: str) -> None:
    payload = json.dumps(
        {
            "protocol_version": "1.0",
            "type": "ack",
            "event_id": event_id,
            "generation": 0,
        },
        separators=(",", ":"),
    ).encode()
    await room.local_participant.publish_data(
        payload,
        reliable=True,
        topic=PRIVATE_TOPIC,
    )


async def _wait_for_application_ack(room, session_id: str) -> dict[str, object]:
    event_id = str(uuid4())
    ack = asyncio.get_running_loop().create_future()

    @room.on("data_received")
    def data_received(packet) -> None:
        if packet.topic != PRIVATE_TOPIC:
            return
        frame = json.loads(bytes(packet.data))
        if frame.get("type") == "ack" and frame.get("event_id") == event_id:
            if not ack.done():
                ack.set_result(frame)

    payload = _core_event(session_id, event_id)
    deadline = asyncio.get_running_loop().time() + STATE_WAIT_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        await room.local_participant.publish_data(
            payload,
            reliable=True,
            topic=APPLICATION_TOPIC,
        )
        try:
            return await asyncio.wait_for(asyncio.shield(ack), timeout=0.25)
        except asyncio.TimeoutError:
            # Backendのparticipant callback確定前に届いた初回だけを再送する。
            pass
    raise TimeoutError("LiveKit application event was not acknowledged")


def test_real_livekit_bootstrap_joins_user_and_character_to_one_room() -> None:
    livekit_url = _required_setting("LIVEKIT_URL")
    api_key = _required_setting("LIVEKIT_API_KEY")
    api_secret = _required_setting("LIVEKIT_API_SECRET")
    backend_url = _required_setting("LIVEKIT_TEST_BACKEND_URL")
    api, rtc = _livekit_sdk()

    async def exercise() -> None:
        async with api.LiveKitAPI(livekit_url, api_key, api_secret) as livekit_api:
            with httpx.Client(base_url=backend_url, timeout=15) as backend:
                conversation_id = _create_conversation(backend)
                bootstrap = _bootstrap(
                    backend,
                    conversation_id=conversation_id,
                    reconnect_grace_ms=60_000,
                )
                assert bootstrap.status_code == 200
                payload = bootstrap.json()
                session_id = payload["session_id"]
                room_name = f"voice-{session_id}"
                user_identity = f"user-{session_id}"
                character_identity = f"character-{CHARACTER_ID}-{session_id}"
                user_room = rtc.Room()

                try:
                    await user_room.connect(payload["livekit_url"], payload["token"])
                    identities = await _wait_for_participants(
                        livekit_api,
                        api,
                        room_name=room_name,
                        expected_identities={user_identity, character_identity},
                    )

                    assert payload["room"] == room_name
                    assert user_room.name == room_name
                    assert user_room.local_participant.identity == user_identity
                    assert set(user_room.remote_participants) == {character_identity}
                    assert identities == {user_identity, character_identity}
                finally:
                    await _cleanup_resources(
                        backend,
                        rooms=(user_room,),
                        session_ids=(session_id,),
                        conversation_id=conversation_id,
                    )

    asyncio.run(exercise())


def test_real_livekit_rejects_expired_token_and_accepts_reissued_token() -> None:
    livekit_url = _required_setting("LIVEKIT_URL")
    api_key = _required_setting("LIVEKIT_API_KEY")
    api_secret = _required_setting("LIVEKIT_API_SECRET")
    backend_url = _required_setting("LIVEKIT_TEST_BACKEND_URL")
    api, rtc = _livekit_sdk()
    from app.livekit_transport.token import LiveKitTokenSigner

    async def exercise() -> None:
        async with api.LiveKitAPI(livekit_url, api_key, api_secret) as livekit_api:
            with httpx.Client(base_url=backend_url, timeout=15) as backend:
                conversation_id = _create_conversation(backend)
                initial = _bootstrap(
                    backend, conversation_id=conversation_id, reconnect_grace_ms=60_000
                )
                assert initial.status_code == 200
                binding = initial.json()
                session_id = binding["session_id"]
                identity = f"user-{session_id}"
                regular = await LiveKitTokenSigner(
                    api_key=api_key,
                    api_secret=api_secret,
                ).issue(
                    identity=identity,
                    room=binding["room"],
                    ttl_seconds=90,
                    grant={"room_join": True, "can_subscribe": True},
                )
                import jwt

                expired_claims = jwt.decode(
                    regular,
                    options={"verify_signature": False},
                )
                # Serverのclock-skew許容範囲より十分古いtokenで失効を検証する。
                expired_claims["nbf"] = int(datetime.now(UTC).timestamp()) - 7_200
                expired_claims["exp"] = int(datetime.now(UTC).timestamp()) - 3_600
                expired = jwt.encode(expired_claims, api_secret, algorithm="HS256")
                expired_room = rtc.Room()
                fresh_room = rtc.Room()
                try:
                    with pytest.raises(rtc.ConnectError):
                        await expired_room.connect(livekit_url, expired)
                    participants = await livekit_api.room.list_participants(
                        api.ListParticipantsRequest(room=binding["room"])
                    )
                    assert identity not in {p.identity for p in participants.participants}
                    reissued = _bootstrap(
                        backend,
                        conversation_id=conversation_id,
                        reconnect_grace_ms=60_000,
                        session_id=session_id,
                    )
                    assert reissued.status_code == 200
                    await fresh_room.connect(livekit_url, reissued.json()["token"])
                    joined = await _wait_for_participants(
                        livekit_api,
                        api,
                        room_name=binding["room"],
                        expected_identities={
                            identity,
                            f"character-{CHARACTER_ID}-{session_id}",
                        },
                    )
                    assert identity in joined
                finally:
                    await _cleanup_resources(
                        backend,
                        rooms=(expired_room, fresh_room),
                        session_ids=(session_id,),
                        conversation_id=conversation_id,
                    )

    asyncio.run(exercise())


def test_real_livekit_user_grant_excludes_camera_publication() -> None:
    livekit_url = _required_setting("LIVEKIT_URL")
    api_key = _required_setting("LIVEKIT_API_KEY")
    api_secret = _required_setting("LIVEKIT_API_SECRET")
    backend_url = _required_setting("LIVEKIT_TEST_BACKEND_URL")
    api, rtc = _livekit_sdk()
    async def exercise() -> None:
        async with api.LiveKitAPI(livekit_url, api_key, api_secret) as livekit_api:
            with httpx.Client(base_url=backend_url, timeout=15) as backend:
                conversation_id = _create_conversation(backend)
                response = _bootstrap(
                    backend, conversation_id=conversation_id, reconnect_grace_ms=60_000
                )
                assert response.status_code == 200
                binding = response.json()
                session_id = binding["session_id"]
                user_room = rtc.Room()
                try:
                    await user_room.connect(livekit_url, binding["token"])
                    participants = await livekit_api.room.list_participants(
                        api.ListParticipantsRequest(room=binding["room"])
                    )
                    user = next(
                        participant
                        for participant in participants.participants
                        if participant.identity == f"user-{session_id}"
                    )
                    assert user.permission.can_publish_sources == [
                        api.TrackSource.MICROPHONE
                    ]
                    assert api.TrackSource.CAMERA not in (
                        user.permission.can_publish_sources
                    )
                    assert all(
                        publication.source != rtc.TrackSource.SOURCE_CAMERA
                        for publication in user.tracks
                    )
                finally:
                    await _cleanup_resources(
                        backend,
                        rooms=(user_room,),
                        session_ids=(session_id,),
                        conversation_id=conversation_id,
                    )

    asyncio.run(exercise())


def test_real_livekit_application_event_receives_private_ack() -> None:
    livekit_url = _required_setting("LIVEKIT_URL")
    backend_url = _required_setting("LIVEKIT_TEST_BACKEND_URL")
    _required_setting("LIVEKIT_API_KEY")
    _required_setting("LIVEKIT_API_SECRET")
    _, rtc = _livekit_sdk()

    async def exercise() -> None:
        with httpx.Client(base_url=backend_url, timeout=15) as backend:
            conversation_id = _create_conversation(backend)
            response = _bootstrap(
                backend, conversation_id=conversation_id, reconnect_grace_ms=60_000
            )
            assert response.status_code == 200
            binding = response.json()
            session_id = binding["session_id"]
            event_id = str(uuid4())
            room = rtc.Room()
            ack = asyncio.get_running_loop().create_future()

            @room.on("data_received")
            def data_received(packet) -> None:
                if packet.topic != PRIVATE_TOPIC:
                    return
                frame = json.loads(bytes(packet.data))
                if frame.get("type") == "ack" and frame.get("event_id") == event_id:
                    if not ack.done():
                        ack.set_result(frame)

            try:
                await room.connect(livekit_url, binding["token"])
                payload = json.dumps(
                    {
                        "protocol_version": "1.0",
                        "event_id": event_id,
                        "type": "session_started",
                        "session_id": session_id,
                        "monotonic_timestamp_ms": 1000,
                        "reconnect_grace_ms": 60_000,
                    },
                    separators=(",", ":"),
                ).encode()
                frame = None
                deadline = (
                    asyncio.get_running_loop().time()
                    + STATE_WAIT_TIMEOUT_SECONDS
                )
                while frame is None and asyncio.get_running_loop().time() < deadline:
                    await room.local_participant.publish_data(
                        payload,
                        reliable=True,
                        topic=APPLICATION_TOPIC,
                    )
                    try:
                        frame = await asyncio.wait_for(
                            asyncio.shield(ack), timeout=0.25
                        )
                    except asyncio.TimeoutError:
                        # participant callbackの確定前に届いた初回だけを再送する。
                        pass
                assert frame is not None
                assert frame["generation"] == 0
            finally:
                await _cleanup_resources(
                    backend,
                    rooms=(room,),
                    session_ids=(session_id,),
                    conversation_id=conversation_id,
                )

    asyncio.run(exercise())


def test_real_livekit_first_ack_stops_backend_outbox_retry(client) -> None:
    _required_setting("LIVEKIT_URL")
    _required_setting("LIVEKIT_API_KEY")
    _required_setting("LIVEKIT_API_SECRET")
    _, rtc = _livekit_sdk()

    async def exercise() -> None:
        conversation_id, binding, room, payloads = await _connect_test_app_user(
            client, rtc
        )
        session_id = binding["session_id"]
        event_id = str(uuid4())
        payload = _core_event(session_id, event_id)
        try:
            _send_core_from_test_app(client, session_id, payload)
            first = await asyncio.wait_for(
                payloads.get(), timeout=STATE_WAIT_TIMEOUT_SECONDS
            )
            await _acknowledge_application_event(room, event_id)

            assert first == payload
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(payloads.get(), timeout=1.25)
        finally:
            await _cleanup_resources(
                client,
                rooms=(room,),
                session_ids=(session_id,),
                conversation_id=conversation_id,
            )

    asyncio.run(exercise())


def test_real_livekit_ack_after_first_retry_stops_remaining_retries(client) -> None:
    _required_setting("LIVEKIT_URL")
    _required_setting("LIVEKIT_API_KEY")
    _required_setting("LIVEKIT_API_SECRET")
    _, rtc = _livekit_sdk()

    async def exercise() -> None:
        conversation_id, binding, room, payloads = await _connect_test_app_user(
            client, rtc
        )
        session_id = binding["session_id"]
        event_id = str(uuid4())
        payload = _core_event(session_id, event_id)
        try:
            _send_core_from_test_app(client, session_id, payload)
            first = await asyncio.wait_for(
                payloads.get(), timeout=STATE_WAIT_TIMEOUT_SECONDS
            )
            retry = await asyncio.wait_for(
                payloads.get(), timeout=STATE_WAIT_TIMEOUT_SECONDS
            )
            await _acknowledge_application_event(room, event_id)

            assert first == retry == payload
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(payloads.get(), timeout=1.25)
        finally:
            await _cleanup_resources(
                client,
                rooms=(room,),
                session_ids=(session_id,),
                conversation_id=conversation_id,
            )

    asyncio.run(exercise())


def test_real_livekit_retry_exhaustion_keeps_room_until_terminal_cleanup(client) -> None:
    _required_setting("LIVEKIT_URL")
    _required_setting("LIVEKIT_API_KEY")
    _required_setting("LIVEKIT_API_SECRET")
    _, rtc = _livekit_sdk()

    async def exercise() -> None:
        conversation_id, binding, room, payloads = await _connect_test_app_user(
            client, rtc
        )
        session_id = binding["session_id"]
        payload = _core_event(session_id, str(uuid4()))
        try:
            _send_core_from_test_app(client, session_id, payload)
            deliveries = [
                await asyncio.wait_for(
                    payloads.get(), timeout=STATE_WAIT_TIMEOUT_SECONDS
                )
                for _ in range(4)
            ]
            reissue = _bootstrap(
                client,
                conversation_id=conversation_id,
                reconnect_grace_ms=60_000,
                session_id=session_id,
            )

            assert deliveries == [payload] * 4
            assert reissue.status_code == 200
            assert reissue.json()["session_id"] == session_id
            assert room.name == binding["room"]
        finally:
            await _cleanup_resources(
                client,
                rooms=(room,),
                session_ids=(session_id,),
                conversation_id=conversation_id,
            )

    asyncio.run(exercise())


def test_real_livekit_outbox_overflow_keeps_room_until_terminal_cleanup(client) -> None:
    livekit_url = _required_setting("LIVEKIT_URL")
    api_key = _required_setting("LIVEKIT_API_KEY")
    api_secret = _required_setting("LIVEKIT_API_SECRET")
    api, rtc = _livekit_sdk()
    from app.livekit_transport.outbox import OutboxCapacityExceeded

    async def exercise() -> None:
        async with api.LiveKitAPI(livekit_url, api_key, api_secret) as livekit_api:
            conversation_id, binding, room, _ = await _connect_test_app_user(
                client, rtc
            )
            session_id = binding["session_id"]
            try:
                for _ in range(256):
                    _send_core_from_test_app(
                        client,
                        session_id,
                        _core_event(session_id, str(uuid4())),
                    )
                with pytest.raises(OutboxCapacityExceeded):
                    _send_core_from_test_app(
                        client,
                        session_id,
                        _core_event(session_id, str(uuid4())),
                    )

                identities = await _wait_for_participants(
                    livekit_api,
                    api,
                    room_name=binding["room"],
                    expected_identities={
                        f"user-{session_id}",
                        f"character-{CHARACTER_ID}-{session_id}",
                    },
                )
                assert identities == {
                    f"user-{session_id}",
                    f"character-{CHARACTER_ID}-{session_id}",
                }
            finally:
                await _cleanup_resources(
                    client,
                    rooms=(room,),
                    session_ids=(session_id,),
                    conversation_id=conversation_id,
                )

    asyncio.run(exercise())


def test_real_livekit_duplicate_identity_keeps_new_connection_and_session() -> None:
    livekit_url = _required_setting("LIVEKIT_URL")
    api_key = _required_setting("LIVEKIT_API_KEY")
    api_secret = _required_setting("LIVEKIT_API_SECRET")
    backend_url = _required_setting("LIVEKIT_TEST_BACKEND_URL")
    api, rtc = _livekit_sdk()

    async def exercise() -> None:
        async with api.LiveKitAPI(livekit_url, api_key, api_secret) as livekit_api:
            with httpx.Client(base_url=backend_url, timeout=15) as backend:
                conversation_id = _create_conversation(backend)
                response = _bootstrap(
                    backend, conversation_id=conversation_id, reconnect_grace_ms=60_000
                )
                assert response.status_code == 200
                binding = response.json()
                session_id = binding["session_id"]
                first = rtc.Room()
                second = rtc.Room()
                disconnected = asyncio.get_running_loop().create_future()

                @first.on("disconnected")
                def first_disconnected(*_: object) -> None:
                    if not disconnected.done():
                        disconnected.set_result(True)

                try:
                    await first.connect(livekit_url, binding["token"])
                    await second.connect(livekit_url, binding["token"])
                    await asyncio.wait_for(
                        disconnected, timeout=STATE_WAIT_TIMEOUT_SECONDS
                    )
                    identities = await _wait_for_participants(
                        livekit_api,
                        api,
                        room_name=binding["room"],
                        expected_identities={
                            f"user-{session_id}",
                            f"character-{CHARACTER_ID}-{session_id}",
                        },
                    )
                    assert second.local_participant.identity == f"user-{session_id}"
                    assert identities == {
                        f"user-{session_id}",
                        f"character-{CHARACTER_ID}-{session_id}",
                    }
                    reissue = _bootstrap(
                        backend,
                        conversation_id=conversation_id,
                        reconnect_grace_ms=60_000,
                        session_id=session_id,
                    )
                    assert reissue.status_code == 200
                    assert reissue.json()["session_id"] == session_id
                finally:
                    await _cleanup_resources(
                        backend,
                        rooms=(first, second),
                        session_ids=(session_id,),
                        conversation_id=conversation_id,
                    )

    asyncio.run(exercise())


def test_real_livekit_grace_timeout_cleans_up_and_requires_a_new_session() -> None:
    livekit_url = _required_setting("LIVEKIT_URL")
    api_key = _required_setting("LIVEKIT_API_KEY")
    api_secret = _required_setting("LIVEKIT_API_SECRET")
    backend_url = _required_setting("LIVEKIT_TEST_BACKEND_URL")
    api, rtc = _livekit_sdk()

    async def exercise() -> None:
        async with api.LiveKitAPI(livekit_url, api_key, api_secret) as livekit_api:
            with httpx.Client(base_url=backend_url, timeout=15) as backend:
                conversation_id = _create_conversation(backend)
                first = _bootstrap(
                    backend,
                    conversation_id=conversation_id,
                    reconnect_grace_ms=SHORT_RECONNECT_GRACE_MS,
                )
                assert first.status_code == 200
                first_payload = first.json()
                assert (
                    first_payload["reconnect_grace_ms"]
                    == SHORT_RECONNECT_GRACE_MS
                )
                old_session_id = first_payload["session_id"]
                old_room_name = f"voice-{old_session_id}"
                old_user_identity = f"user-{old_session_id}"
                old_character_identity = (
                    f"character-{CHARACTER_ID}-{old_session_id}"
                )
                old_user_room = rtc.Room()
                fresh_user_room = rtc.Room()
                fresh_session_id: str | None = None

                try:
                    await old_user_room.connect(
                        first_payload["livekit_url"], first_payload["token"]
                    )
                    joined = await _wait_for_participants(
                        livekit_api,
                        api,
                        room_name=old_room_name,
                        expected_identities={
                            old_user_identity,
                            old_character_identity,
                        },
                    )
                    assert joined == {old_user_identity, old_character_identity}
                    await _wait_for_application_ack(
                        old_user_room, old_session_id
                    )

                    await old_user_room.disconnect()

                    remaining_rooms = await _wait_for_room_cleanup(
                        livekit_api,
                        api,
                        room_name=old_room_name,
                    )
                    assert remaining_rooms == []

                    expired_reconnect = _bootstrap(
                        backend,
                        conversation_id=conversation_id,
                        reconnect_grace_ms=SHORT_RECONNECT_GRACE_MS,
                        session_id=old_session_id,
                    )
                    assert expired_reconnect.status_code == 409
                    assert expired_reconnect.json()["detail"]["code"] == (
                        "session_not_reconnectable"
                    )

                    fresh = _bootstrap(
                        backend,
                        conversation_id=conversation_id,
                        reconnect_grace_ms=SHORT_RECONNECT_GRACE_MS,
                    )
                    assert fresh.status_code == 200
                    fresh_payload = fresh.json()
                    fresh_session_id = fresh_payload["session_id"]
                    assert fresh_session_id != old_session_id

                    await fresh_user_room.connect(
                        fresh_payload["livekit_url"], fresh_payload["token"]
                    )
                    fresh_identities = await _wait_for_participants(
                        livekit_api,
                        api,
                        room_name=f"voice-{fresh_session_id}",
                        expected_identities={
                            f"user-{fresh_session_id}",
                            f"character-{CHARACTER_ID}-{fresh_session_id}",
                        },
                    )
                    assert fresh_identities == {
                        f"user-{fresh_session_id}",
                        f"character-{CHARACTER_ID}-{fresh_session_id}",
                    }
                finally:
                    await _cleanup_resources(
                        backend,
                        rooms=(old_user_room, fresh_user_room),
                        session_ids=(old_session_id, fresh_session_id),
                        conversation_id=conversation_id,
                    )

    asyncio.run(exercise())
