from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from jsonschema import Draft202012Validator, FormatChecker


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GATE_PATH = REPOSITORY_ROOT / "backend/evals/screen_reference/gate.py"
SCHEMA_PATH = REPOSITORY_ROOT / "docs/schemas/screen-reference-evaluation-v1.schema.json"


def test_synthetic_reference_gate_meets_adr_thresholds(tmp_path: Path) -> None:
    output = tmp_path / "screen-reference-evaluation.json"
    result = subprocess.run(
        [
            sys.executable,
            str(GATE_PATH),
            "--rule-iterations",
            "20",
            "--output",
            str(output),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["passed"] is True
    assert artifact["metrics"]["case_pass_rate"] == 1.0
    assert artifact["metrics"]["prohibited_or_unauthorized_inspections"] == 0
    assert artifact["metrics"]["judge_call_rate"] <= 0.20
    assert all("current_message" not in case for case in artifact["cases"])


def test_reference_evaluation_artifact_matches_metadata_only_schema(tmp_path: Path) -> None:
    output = tmp_path / "screen-reference-evaluation.json"
    subprocess.run(
        [sys.executable, str(GATE_PATH), "--output", str(output)],
        cwd=REPOSITORY_ROOT,
        check=True,
    )
    artifact = json.loads(output.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(artifact)
    case_keys = {key for case in artifact["cases"] for key in case}
    assert case_keys.isdisjoint(
        {"current_message", "history", "content", "observation", "window_title", "image"}
    )

    fixture = json.loads(
        (
            REPOSITORY_ROOT
            / "contracts/perception/screen/fixtures/contextual-reference-cases.json"
        ).read_text(encoding="utf-8")
    )
    serialized = json.dumps(artifact, ensure_ascii=False)
    for case in fixture["cases"]:
        assert case["current_message"] not in serialized
