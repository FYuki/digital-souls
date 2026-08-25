import importlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from jsonschema import Draft202012Validator


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "voice-session"
FIXTURE_ROOT = CONTRACT_ROOT / "fixtures"


def _load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _fixture(name: str) -> dict[str, object]:
    loaded = _load_json(FIXTURE_ROOT / name)
    assert isinstance(loaded, dict)
    return loaded


def _validator() -> Draft202012Validator:
    schema = _load_json(CONTRACT_ROOT / "voice-session.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _normal_response_event(event_type: str) -> dict[str, object]:
    events = _fixture("normal.json")["events"]
    assert isinstance(events, list)
    event = next(event for event in events if event["type"] == event_type)
    assert isinstance(event, dict)
    return event


def test_schema_accepts_every_event_from_normal_fixture() -> None:
    events = _fixture("normal.json")["events"]
    assert isinstance(events, list)
    validator = _validator()
    for event in events:
        validator.validate(event)


def test_normal_fixture_separates_speech_stop_from_held_response_start() -> None:
    fixture = _fixture("normal.json")
    events = fixture["events"]
    expected = fixture["expected"]
    assert isinstance(events, list)
    assert isinstance(expected, dict)

    pending_utterance_id = expected["pending_utterance_id"]
    response_source_utterance_ids = expected["response_source_utterance_ids"]
    stopped_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "speech_stopped"
        and event["utterance_id"] == pending_utterance_id
    )
    pending_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "utterance_pending"
        and event["utterance_id"] == pending_utterance_id
    )
    response_index, response_started = next(
        (index, event)
        for index, event in enumerate(events)
        if event["type"] == "response_started"
    )

    assert stopped_index < pending_index < response_index
    assert not any(
        event["type"] == "response_started"
        for event in events[stopped_index + 1 : pending_index + 1]
    )
    assert response_started["source_utterance_ids"] == response_source_utterance_ids
    assert pending_utterance_id in response_started["source_utterance_ids"]


def test_observation_rejects_content_and_audio_payload_fields() -> None:
    events = _fixture("normal.json")["events"]
    assert isinstance(events, list)
    observation = next(event for event in events if event["type"] == "observation")
    validator = _validator()
    for forbidden_field in ("transcript", "assistant_content", "audio"):
        invalid = {**observation, forbidden_field: "保存してはいけない値"}
        assert list(validator.iter_errors(invalid)), forbidden_field


def test_backend_boundary_parses_the_shared_normal_and_cancel_fixtures() -> None:
    parser = importlib.import_module("app.voice_session.validation")
    for fixture_name in ("normal.json", "cancel-race.json"):
        events = _fixture(fixture_name)["events"]
        assert isinstance(events, list)
        assert [parser.parse_voice_session_event(event) for event in events]


@pytest.mark.parametrize("event_type", ["response_delta", "response_audio_chunk"])
def test_backend_boundary_accepts_ordered_and_empty_text_ranges(
    event_type: str,
) -> None:
    parser = importlib.import_module("app.voice_session.validation")
    event = _normal_response_event(event_type)

    parsed = parser.parse_voice_session_event(event)
    empty_range_event = {**event, "text_range": {"start": 4, "end": 4}}
    empty_range_parsed = parser.parse_voice_session_event(empty_range_event)

    assert parsed.text_range is not None
    assert (parsed.text_range.start, parsed.text_range.end) == (0, 4)
    assert empty_range_parsed.text_range is not None
    assert (
        empty_range_parsed.text_range.start,
        empty_range_parsed.text_range.end,
    ) == (4, 4)
    assert event["text_range"] == {"start": 0, "end": 4}


@pytest.mark.parametrize("event_type", ["response_delta", "response_audio_chunk"])
def test_backend_boundary_rejects_reversed_text_ranges(event_type: str) -> None:
    parser = importlib.import_module("app.voice_session.validation")
    event = _normal_response_event(event_type)
    reversed_range_event = {**event, "text_range": {"start": 4, "end": 3}}

    with pytest.raises(ValueError):
        parser.parse_voice_session_event(reversed_range_event)

    assert event["text_range"] == {"start": 0, "end": 4}


def test_cancelled_response_discards_late_delta_without_changing_state() -> None:
    fixture = _fixture("cancel-race.json")
    events = fixture["events"]
    expected = fixture["expected"]
    assert isinstance(events, list)
    assert isinstance(expected, dict)
    state = {"terminal": None, "text": ""}
    state_before_late_event = None
    for event in events:
        if state["terminal"] is not None:
            state_before_late_event = state.copy()
            continue
        if event["type"] == "response_delta":
            state["text"] += event["text"]
        elif event["type"] in {
            "response_cancelled", "response_completed", "response_failed"
        }:
            state["terminal"] = event["type"]
    assert state_before_late_event is not None
    assert state == state_before_late_event
    assert state["terminal"] == expected["terminal_event"]
    assert expected["late_event_discarded"] is True


def test_protocol_mismatch_is_rejected_at_parser_boundary() -> None:
    parser = importlib.import_module("app.voice_session.validation")
    fixture = _fixture("protocol-version-mismatch.json")
    with pytest.raises(ValueError):
        parser.parse_voice_session_event(fixture["event"])


def test_standard_codegen_command_reproduces_both_committed_generated_files(
    tmp_path: Path,
) -> None:
    frontend_modules = REPOSITORY_ROOT / "frontend" / "node_modules"
    if not frontend_modules.is_dir():
        pytest.skip("Frontendの標準依存が準備されていません")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for relative_path in ("package.json", "frontend/package.json"):
        source = REPOSITORY_ROOT / relative_path
        destination = checkout / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for relative_path in (
        "frontend/package-lock.json",
        "contracts/voice-session",
        "backend/app/voice_session",
        "frontend/src/lib/voice-session",
    ):
        source = REPOSITORY_ROOT / relative_path
        destination = checkout / relative_path
        if source.is_dir():
            shutil.copytree(source, destination)
        elif source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    (checkout / "frontend" / "node_modules").symlink_to(
        frontend_modules,
        target_is_directory=True,
    )

    generated_paths = (
        Path("backend/app/voice_session/generated.py"),
        Path("frontend/src/lib/voice-session/generated.ts"),
    )
    committed = {
        path: (checkout / path).read_bytes() if (checkout / path).is_file() else None
        for path in generated_paths
    }

    completed = subprocess.run(
        ["npm", "run", "generate:voice-session"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert {
        path: (checkout / path).read_bytes() if (checkout / path).is_file() else None
        for path in generated_paths
    } == committed


@pytest.mark.parametrize(
    ("chunks", "last_played_sequence", "expected"),
    [
        (
            [
                {"sequence": 1, "text_range": {"start": 0, "end": 2}},
                {"sequence": 2, "text_range": {"start": 2, "end": 4}},
                {"sequence": 3, "text_range": {"start": 4, "end": 6}},
            ],
            2,
            "光🌟織の",
        ),
        (
            [
                {"sequence": 1, "text_range": {"start": 0, "end": 2}},
                {"sequence": 3, "text_range": {"start": 4, "end": 6}},
            ],
            3,
            "光🌟",
        ),
        ([], 0, ""),
    ],
)
def test_played_text_prefix_uses_only_contiguous_completed_sequences(
    chunks: list[dict[str, object]],
    last_played_sequence: int,
    expected: str,
) -> None:
    playback_range = importlib.import_module("app.voice_session.playback_range")

    result = playback_range.played_text_prefix(
        "光🌟織の返事",
        chunks,
        last_played_sequence=last_played_sequence,
    )

    assert result == expected
