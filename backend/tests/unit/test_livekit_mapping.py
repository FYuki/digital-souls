from __future__ import annotations

import importlib

import pytest


def _mapping_module(contract: str):
    module_name = "app.livekit_transport.mapping"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is None or not (
            error.name == module_name or module_name.startswith(f"{error.name}.")
        ):
            raise
    pytest.fail(f"{module_name} must implement {contract}")


def test_core_notification_contains_no_livekit_transport_metadata() -> None:
    module = _mapping_module("transport metadata isolation from Core notifications")
    adapter = module.ParticipantMapping()
    adapter.bind(
        core_participant_id="40000000-0000-4000-8000-000000000010",
        identity="user-20000000-0000-4000-8000-000000000010",
        participant_sid="PA_transport_private",
        room_sid="RM_transport_private",
    )

    notification = adapter.core_notification(
        identity="user-20000000-0000-4000-8000-000000000010",
        event_type="session_disconnected",
        track_id="TR_transport_private",
        room_name="voice-20000000-0000-4000-8000-000000000010",
        participant_metadata={"livekit": "private"},
    )

    assert notification == {
        "participant_id": "40000000-0000-4000-8000-000000000010",
        "type": "session_disconnected",
    }


def test_duplicate_identity_replaces_connection_without_ending_session() -> None:
    module = _mapping_module("last-join-wins connection replacement")
    adapter = module.ParticipantMapping()
    identity = "user-20000000-0000-4000-8000-000000000010"
    adapter.bind(
        core_participant_id="40000000-0000-4000-8000-000000000010",
        identity=identity,
        participant_sid="PA_old",
        room_sid="RM_one",
    )

    replacement = adapter.replace_connection(identity, "PA_new")

    assert replacement.disconnected_participant_sid == "PA_old"
    assert replacement.active_participant_sid == "PA_new"
    assert replacement.session_ended is False
