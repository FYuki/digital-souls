from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GATE_PATH = REPOSITORY_ROOT / "backend" / "evals" / "privacy_classifier" / "gate.py"
RESULTS_FIXTURE = (
    REPOSITORY_ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "semantic_privacy_eval_results.json"
)
PROFILES = ("ADMISSION", "QUERY_GATE")
METRICS = ("abstain_rate", "false_negative_rate", "false_positive_rate")


def _write_cases(path: Path) -> None:
    cases = [
        {
            "id": "sensitive-1",
            "text": "sensitive one",
            "vars": {"text": "sensitive one", "case_id": "sensitive-1"},
            "sensitive": True,
            "expected_subject_scope": "SELF",
            "allowed_classifications": ["SENSITIVE", "ABSTAIN"],
        },
        {
            "id": "sensitive-2",
            "text": "sensitive two",
            "vars": {"text": "sensitive two", "case_id": "sensitive-2"},
            "sensitive": True,
            "expected_subject_scope": "SELF",
            "allowed_classifications": ["SENSITIVE", "ABSTAIN"],
        },
        {
            "id": "safe-1",
            "text": "safe one",
            "vars": {"text": "safe one", "case_id": "safe-1"},
            "sensitive": False,
            "expected_subject_scope": "GENERAL",
            "allowed_classifications": ["NOT_SENSITIVE", "ABSTAIN"],
        },
        {
            "id": "safe-2",
            "text": "safe two",
            "vars": {"text": "safe two", "case_id": "safe-2"},
            "sensitive": False,
            "expected_subject_scope": "GENERAL",
            "allowed_classifications": ["NOT_SENSITIVE", "ABSTAIN"],
        },
    ]
    path.write_text(
        "".join(json.dumps(case) + "\n" for case in cases),
        encoding="utf-8",
    )


def _write_thresholds(path: Path, limit: float) -> None:
    path.write_text(
        json.dumps(
            {profile: {metric: limit for metric in METRICS} for profile in PROFILES}
        ),
        encoding="utf-8",
    )


def _run_gate(
    tmp_path: Path,
    *,
    threshold: float,
    report_only: bool = False,
    filter_first_n: int | None = None,
    results_path: Path = RESULTS_FIXTURE,
) -> subprocess.CompletedProcess[str]:
    cases_path = tmp_path / "cases.jsonl"
    thresholds_path = tmp_path / "thresholds.json"
    _write_cases(cases_path)
    _write_thresholds(thresholds_path, threshold)
    arguments = [
        sys.executable,
        str(GATE_PATH),
        "--results",
        str(results_path),
        "--cases",
        str(cases_path),
        "--thresholds",
        str(thresholds_path),
    ]
    if report_only:
        arguments.append("--report-only")
    if filter_first_n is not None:
        arguments.extend(("--filter-first-n", str(filter_first_n)))
    return subprocess.run(arguments, capture_output=True, text=True)


def _write_conforming_results(path: Path) -> None:
    payload = json.loads(RESULTS_FIXTURE.read_text(encoding="utf-8"))
    corrections = {
        ("ADMISSION", "sensitive-2"): ("SENSITIVE", "SELF"),
        ("ADMISSION", "safe-1"): ("NOT_SENSITIVE", "GENERAL"),
        ("QUERY_GATE", "sensitive-2"): ("SENSITIVE", "SELF"),
        ("QUERY_GATE", "safe-2"): ("NOT_SENSITIVE", "GENERAL"),
    }
    for record in payload["results"]["results"]:
        identity = (record["provider"]["label"], record["vars"]["case_id"])
        if identity in corrections:
            output = json.loads(record["response"]["output"])
            output["classification"], output["subject_scope"] = corrections[identity]
            record["response"]["output"] = json.dumps(output)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _keep_result_cases(source: Path, destination: Path, case_ids: set[str]) -> None:
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["results"]["results"] = [
        record
        for record in payload["results"]["results"]
        if record["vars"]["case_id"] in case_ids
    ]
    destination.write_text(json.dumps(payload), encoding="utf-8")


def _change_result(
    source: Path,
    destination: Path,
    *,
    profile: str,
    case_id: str,
    field: str,
    value: str,
) -> None:
    payload = json.loads(source.read_text(encoding="utf-8"))
    record = next(
        item
        for item in payload["results"]["results"]
        if item["provider"]["label"] == profile and item["vars"]["case_id"] == case_id
    )
    output = json.loads(record["response"]["output"])
    output[field] = value
    record["response"]["output"] = json.dumps(output)
    destination.write_text(json.dumps(payload), encoding="utf-8")


def _profile_line(output: str, profile: str) -> str:
    return next(line for line in output.splitlines() if line.startswith(f"{profile}:"))


@pytest.mark.parametrize(
    ("profile", "expected_values"),
    [
        (
            "ADMISSION",
            {
                "cases": "4",
                "case_failures": "2",
                "abstain_rate": "0.250000",
                "false_negative_rate": "0.500000",
                "false_positive_rate": "0.500000",
                "latency_median": "2.500000",
                "latency_p95": "100.000000",
            },
        ),
        (
            "QUERY_GATE",
            {
                "cases": "4",
                "case_failures": "2",
                "abstain_rate": "0.250000",
                "false_negative_rate": "0.000000",
                "false_positive_rate": "0.500000",
                "latency_median": "0.250000",
                "latency_p95": "0.400000",
            },
        ),
    ],
)
def test_gate_reports_defined_metrics_per_provider_label(
    tmp_path: Path,
    profile: str,
    expected_values: dict[str, str],
) -> None:
    result = _run_gate(tmp_path, threshold=0.5)

    assert result.returncode != 0
    profile_line = _profile_line(result.stdout, profile)
    for name, value in expected_values.items():
        assert f"{name}={value}" in profile_line


def test_gate_succeeds_when_rates_are_at_their_upper_limits(tmp_path: Path) -> None:
    results_path = tmp_path / "conforming-results.json"
    _write_conforming_results(results_path)

    result = _run_gate(tmp_path, threshold=0.25, results_path=results_path)

    assert result.returncode == 0, result.stderr


def test_gate_rejects_a_case_missing_from_both_profiles(tmp_path: Path) -> None:
    conforming_path = tmp_path / "conforming-results.json"
    results_path = tmp_path / "missing-case-results.json"
    _write_conforming_results(conforming_path)
    _keep_result_cases(
        conforming_path,
        results_path,
        {"sensitive-1", "sensitive-2", "safe-1"},
    )

    result = _run_gate(tmp_path, threshold=0.5, results_path=results_path)

    assert result.returncode != 0


def test_gate_accepts_the_expected_first_case_for_a_partial_run(
    tmp_path: Path,
) -> None:
    results_path = tmp_path / "first-case-results.json"
    _keep_result_cases(RESULTS_FIXTURE, results_path, {"sensitive-1"})

    result = _run_gate(
        tmp_path,
        threshold=1.0,
        filter_first_n=1,
        results_path=results_path,
    )

    assert result.returncode == 0, result.stderr
    assert "ADMISSION: cases=1" in result.stdout
    assert "QUERY_GATE: cases=1" in result.stdout


def test_gate_fails_when_a_rate_exceeds_its_upper_limit(tmp_path: Path) -> None:
    results_path = tmp_path / "conforming-results.json"
    _write_conforming_results(results_path)

    result = _run_gate(tmp_path, threshold=0.24, results_path=results_path)

    assert result.returncode != 0


def test_gate_fails_for_disallowed_classification_within_rate_limits(
    tmp_path: Path,
) -> None:
    conforming_path = tmp_path / "conforming-results.json"
    results_path = tmp_path / "classification-mismatch-results.json"
    _write_conforming_results(conforming_path)
    _change_result(
        conforming_path,
        results_path,
        profile="ADMISSION",
        case_id="safe-2",
        field="classification",
        value="SENSITIVE",
    )

    result = _run_gate(tmp_path, threshold=0.5, results_path=results_path)

    assert result.returncode != 0
    assert "ADMISSION.case_failures=1" in result.stderr


def test_gate_fails_for_non_abstain_subject_scope_mismatch(
    tmp_path: Path,
) -> None:
    conforming_path = tmp_path / "conforming-results.json"
    results_path = tmp_path / "scope-mismatch-results.json"
    _write_conforming_results(conforming_path)
    _change_result(
        conforming_path,
        results_path,
        profile="QUERY_GATE",
        case_id="sensitive-2",
        field="subject_scope",
        value="THIRD_PARTY",
    )

    result = _run_gate(tmp_path, threshold=0.5, results_path=results_path)

    assert result.returncode != 0
    assert "QUERY_GATE.case_failures=1" in result.stderr


def test_latency_statistics_do_not_fail_conformance(tmp_path: Path) -> None:
    results_path = tmp_path / "conforming-results.json"
    _write_conforming_results(results_path)

    result = _run_gate(tmp_path, threshold=0.5, results_path=results_path)

    assert result.returncode == 0, result.stderr
    assert "latency_p95=100.000000" in _profile_line(result.stdout, "ADMISSION")


def test_report_only_skips_quality_threshold_failure(tmp_path: Path) -> None:
    result = _run_gate(tmp_path, threshold=0.0, report_only=True)

    assert result.returncode == 0, result.stderr
    assert _profile_line(result.stdout, "ADMISSION")
    assert _profile_line(result.stdout, "QUERY_GATE")


@pytest.mark.parametrize(
    "mismatch", ["case_id", "profile", "model_digest", "policy_version"]
)
def test_gate_rejects_results_that_cannot_share_one_evaluation_context(
    tmp_path: Path,
    mismatch: str,
) -> None:
    conforming_path = tmp_path / "conforming-results.json"
    _write_conforming_results(conforming_path)
    valid_result = _run_gate(tmp_path, threshold=0.5, results_path=conforming_path)

    assert valid_result.returncode == 0, valid_result.stderr

    payload = json.loads(conforming_path.read_text(encoding="utf-8"))
    record = payload["results"]["results"][0]
    output = json.loads(record["response"]["output"])
    if mismatch == "case_id":
        output["case_id"] = "different-case"
    elif mismatch == "profile":
        output["profile"] = "QUERY_GATE"
    elif mismatch == "model_digest":
        output["model_digest"] = "sha256:different"
    else:
        output["policy_version"] = "different-policy"
    record["response"]["output"] = json.dumps(output)
    results_path = tmp_path / "mismatched-results.json"
    results_path.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_gate(tmp_path, threshold=0.5, results_path=results_path)

    assert result.returncode != 0
