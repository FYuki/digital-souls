from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = (
    REPOSITORY_ROOT
    / "contracts"
    / "livekit-transport"
    / "livekit-transport.schema.json"
)


def _validator() -> Draft202012Validator:
    assert SCHEMA_PATH.is_file(), "LiveKit transport private schema must be added"
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


@pytest.mark.parametrize(
    "frame",
    [
        {
            "protocol_version": "1.0",
            "type": "ack",
            "event_id": "10000000-0000-4000-8000-000000000010",
            "generation": 0,
        },
        {
            "protocol_version": "1.0",
            "type": "state_sync_request",
            "generation": 1,
        },
        {
            "protocol_version": "1.0",
            "type": "authoritative_state",
            "generation": 1,
            "session_phase": "available",
            "terminal_outcomes": [],
        },
        {
            "protocol_version": "1.0",
            "type": "authoritative_state",
            "generation": 1,
            "session_phase": "available",
            "terminal_outcomes": [
                {
                    "type": "response_interrupted",
                    "session_id": "20000000-0000-4000-8000-000000000010",
                    "response_id": "30000000-0000-4000-8000-000000000010",
                    "confirmed_audio_sequence": 0,
                }
            ],
        },
        {
            "protocol_version": "1.0",
            "type": "logical_audio_segment",
            "response_id": "30000000-0000-4000-8000-000000000010",
            "audio_sequence": 0,
            "generation": 1,
            "pcm_sample_count": 480,
        },
        {
            "protocol_version": "1.0",
            "type": "microphone_observation",
            "generation": 1,
            "frame_count": 1,
            "sample_count": 480,
            "elapsed_ms": 0,
            "missing_frames": 0,
        },
    ],
)
def test_private_schema_accepts_each_required_transport_frame(
    frame: dict[str, object],
) -> None:
    _validator().validate(frame)


def test_private_schema_rejects_unknown_frames_and_audio_bytes() -> None:
    validator = _validator()
    unknown = {
        "protocol_version": "1.0",
        "type": "livekit_rpc",
        "generation": 0,
    }
    audio_bytes = {
        "protocol_version": "1.0",
        "type": "logical_audio_segment",
        "response_id": "30000000-0000-4000-8000-000000000010",
        "audio_sequence": 0,
        "generation": 0,
        "pcm_sample_count": 480,
        "audio": "base64-audio-must-not-be-a-control-frame",
    }

    assert list(validator.iter_errors(unknown))
    assert list(validator.iter_errors(audio_bytes))


@pytest.mark.parametrize(
    "terminal_outcome",
    [
        {},
        {
            "type": "response_interrupted",
            "session_id": "20000000-0000-4000-8000-000000000010",
            "response_id": "30000000-0000-4000-8000-000000000010",
        },
        {
            "type": "response_interrupted",
            "session_id": "not-a-uuid",
            "response_id": "30000000-0000-4000-8000-000000000010",
            "confirmed_audio_sequence": 0,
        },
        {
            "type": "response_interrupted",
            "session_id": "20000000-0000-4000-8000-000000000010",
            "response_id": "30000000-0000-4000-8000-000000000010",
            "confirmed_audio_sequence": -1,
        },
        {
            "type": "response_interrupted",
            "session_id": "20000000-0000-4000-8000-000000000010",
            "response_id": "30000000-0000-4000-8000-000000000010",
            "confirmed_audio_sequence": 0,
            "unexpected": True,
        },
    ],
    ids=("arbitrary-object", "missing-field", "invalid-uuid", "negative-sequence", "additional-field"),
)
def test_private_schema_rejects_invalid_terminal_outcome(
    terminal_outcome: dict[str, object],
) -> None:
    frame = {
        "protocol_version": "1.0",
        "type": "authoritative_state",
        "generation": 1,
        "session_phase": "available",
        "terminal_outcomes": [terminal_outcome],
    }

    assert list(_validator().iter_errors(frame))
