import importlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "perception" / "screen"
REFERENCE_DECISION_SCHEMA = CONTRACT_ROOT / "screen-reference-decision.schema.json"
GROUNDING_OBSERVATION_SCHEMA = (
    CONTRACT_ROOT / "screen-grounding-observation.schema.json"
)
VALID_FIXTURE_ROOT = CONTRACT_ROOT / "fixtures" / "valid"
INVALID_FIXTURE_ROOT = CONTRACT_ROOT / "fixtures" / "invalid"
EVENT_MODEL_NAMES = {
    "screen_routing_disclosed": "RoutingDisclosure",
    "screen_session_start_requested": "SessionStartRequested",
    "screen_session_started": "SessionStarted",
    "screen_session_heartbeat": "SessionHeartbeat",
    "screen_session_heartbeat_accepted": "SessionHeartbeatAccepted",
    "screen_session_revoke_requested": "SessionRevokeRequested",
    "screen_session_revoked": "SessionRevoked",
    "screen_snapshot_requested": "SnapshotRequested",
    "screen_snapshot_upload_metadata": "SnapshotUploadMetadata",
    "screen_snapshot_upload_accepted": "SnapshotUploadAccepted",
    "screen_status": "ScreenStatus",
    "screen_error": "ScreenError",
}


def _load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _validator() -> Draft202012Validator:
    schema = _load_json(CONTRACT_ROOT / "screen-perception.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _valid_events(name: str) -> list[dict[str, object]]:
    fixture = _load_json(VALID_FIXTURE_ROOT / name)
    assert isinstance(fixture, dict)
    events = fixture["events"]
    assert isinstance(events, list)
    assert all(isinstance(event, dict) for event in events)
    return events


def test_shared_schema_accepts_all_valid_events() -> None:
    validator = _validator()
    for fixture_path in sorted(VALID_FIXTURE_ROOT.glob("*.json")):
        for event in _valid_events(fixture_path.name):
            validator.validate(event)


def test_shared_schema_rejects_all_invalid_events() -> None:
    validator = _validator()
    for fixture_path in sorted(INVALID_FIXTURE_ROOT.glob("*.json")):
        assert list(validator.iter_errors(_load_json(fixture_path))), fixture_path.name


def test_backend_boundary_parses_shared_valid_fixtures() -> None:
    parser = importlib.import_module("app.screen_perception.validation")
    for fixture_path in sorted(VALID_FIXTURE_ROOT.glob("*.json")):
        for event in _valid_events(fixture_path.name):
            parsed = parser.parse_screen_perception_event(event)
            assert type(parsed).__name__ == EVENT_MODEL_NAMES[event["type"]]


def test_generated_variant_model_requires_its_own_fields() -> None:
    generated = importlib.import_module("app.screen_perception.generated")
    with pytest.raises(ValidationError):
        generated.SessionHeartbeat.model_validate(
            {
                "protocol_version": "1.0",
                "type": "screen_session_heartbeat",
                "event_id": "10000000-0000-4000-8000-000000000004",
                "screen_session_id": "40000000-0000-4000-8000-000000000001",
                "generation": 1,
            }
        )


def test_decoded_pixel_limit_is_checked_beyond_per_dimension_limits() -> None:
    parser = importlib.import_module("app.screen_perception.validation")
    metadata = next(
        event
        for event in _valid_events("normal-session.json")
        if event["type"] == "screen_snapshot_upload_metadata"
    )
    with pytest.raises(ValueError, match="pixel limit"):
        parser.parse_screen_perception_event(
            {**metadata, "width": 2560, "height": 2560}
        )


def test_backend_boundary_rejects_calendar_invalid_timestamp() -> None:
    parser = importlib.import_module("app.screen_perception.validation")
    request = next(
        event
        for event in _valid_events("normal-session.json")
        if event["type"] == "screen_snapshot_requested"
    )
    with pytest.raises(ValueError, match="invalid UTC timestamp"):
        parser.parse_screen_perception_event(
            {**request, "requested_at": "2026-02-30T03:00:06Z"}
        )


def test_valid_wire_events_exclude_sensitive_payload_fields() -> None:
    forbidden = {
        "image",
        "image_base64",
        "question",
        "observation",
        "window_title",
        "url",
        "endpoint",
        "credential",
    }
    for fixture_path in sorted(VALID_FIXTURE_ROOT.glob("*.json")):
        for event in _valid_events(fixture_path.name):
            assert forbidden.isdisjoint(event)


def test_revocation_fixture_requires_late_upload_to_be_rejected() -> None:
    fixture = _load_json(VALID_FIXTURE_ROOT / "revocation-race.json")
    assert isinstance(fixture, dict)
    events = fixture["events"]
    expected = fixture["expected"]
    assert isinstance(events, list)
    assert isinstance(expected, dict)
    late_index = expected["late_event_index"]
    assert isinstance(late_index, int)
    assert events[late_index]["type"] == "screen_snapshot_upload_metadata"
    assert expected == {
        "late_event_index": 2,
        "late_event_reason_code": "session_revoked",
        "provider_request_count": 0,
    }


def test_explicit_reference_fixture_has_both_positive_and_negative_cases() -> None:
    fixture = _load_json(CONTRACT_ROOT / "fixtures" / "explicit-reference-cases.json")
    assert isinstance(fixture, dict)
    assert fixture["normalization"] == "NFKC"
    cases = fixture["cases"]
    assert isinstance(cases, list)
    assert any(case["expected"] is True for case in cases)
    assert any(case["expected"] is False for case in cases)
    assert any(case["reason"] == "quoted" for case in cases)
    assert any(case["reason"] == "negated" for case in cases)
    assert any(case["explicit_ui"] is True for case in cases)


def test_contextual_reference_fixture_covers_rules_limited_llm_and_safe_fallback() -> None:
    fixture = _load_json(CONTRACT_ROOT / "fixtures/contextual-reference-cases.json")
    assert isinstance(fixture, dict)
    cases = fixture["cases"]
    assert isinstance(cases, list)
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["expected_decision"] for case in cases} == {
        "answer_without_screen",
        "inspect_screen",
        "clarify_reference",
    }
    assert {case["expected_path"] for case in cases} == {
        "rule",
        "llm",
        "fallback",
    }
    llm_cases = [case for case in cases if case["expected_path"] == "llm"]
    assert len(llm_cases) / len(cases) <= 0.2
    assert all(
        case["expected_decision"] != "inspect_screen"
        for case in cases
        if not case["screen_use_authorized"]
    )
    by_id = {case["id"]: case for case in cases}
    assert by_id["prohibition-overrides-explicit-ui"]["expected_decision"] == (
        "answer_without_screen"
    )
    assert by_id["expired-screen-history-is-not-cloud-judge-input"][
        "expected_cloud_history_items"
    ] == 0


def test_reference_decision_schema_rejects_free_reason_provider_and_target_selection() -> None:
    validator = Draft202012Validator(_load_json(REFERENCE_DECISION_SCHEMA))
    valid = {
        "decision": "clarify_reference",
        "basis": "competing_references",
    }
    validator.validate(valid)
    for extra in ("reason", "provider", "screen_session_id", "target"):
        assert list(validator.iter_errors({**valid, extra: "model-selected"}))
    assert list(
        validator.iter_errors(
            {"decision": "use_tool", "basis": "shared_screen_candidate"}
        )
    )
    assert list(
        validator.iter_errors(
            {"decision": "inspect_screen", "basis": "recent_conversation"}
        )
    )


def test_grounding_observation_fixtures_enforce_candidates_evidence_and_core_ownership() -> None:
    schema = _load_json(GROUNDING_OBSERVATION_SCHEMA)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    fixture = _load_json(CONTRACT_ROOT / "fixtures/grounding-observations.json")
    assert isinstance(fixture, dict)
    valid = fixture["valid"]
    invalid = fixture["invalid"]
    assert isinstance(valid, list)
    assert isinstance(invalid, list)
    for case in valid:
        validator.validate(case["observation"])
    for case in invalid:
        assert list(validator.iter_errors(case["observation"])), case["id"]
    assert {case["id"] for case in valid} == {
        "single-warning",
        "multiple-red-candidates",
        "target-disappeared",
        "small-text-unreadable",
        "unrelated-screen",
    }


def test_codegen_command_reproduces_committed_screen_types(tmp_path: Path) -> None:
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
        "frontend/scripts/generate-screen-perception.mjs",
        "contracts/perception/screen",
        "backend/app/screen_perception",
        "frontend/src/lib/screen-perception",
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
        Path("backend/app/screen_perception/generated.py"),
        Path("frontend/src/lib/screen-perception/generated.ts"),
    )
    committed = {path: (checkout / path).read_bytes() for path in generated_paths}
    completed = subprocess.run(
        ["npm", "run", "generate:screen-perception"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert {
        path: (checkout / path).read_bytes() for path in generated_paths
    } == committed
