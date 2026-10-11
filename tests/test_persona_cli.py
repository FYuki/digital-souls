"""Offline CLI reports failures honestly and keeps body output opt-in."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools/evaluate-persona-consistency.py"
pytestmark = pytest.mark.it1


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=30, check=False
    )


@pytest.mark.parametrize(
    "options",
    [
        ("--runs", "0"),
        ("--runs", "-1"),
        ("--profile", str(ROOT / "evals/persona/local-profile.example.json")),
    ],
)
def test_invalid_or_real_profile_never_runs(options: tuple[str, ...]) -> None:
    result = run(*options)
    assert result.returncode == 1 and not result.stdout
    assert "人格評価に失敗" in result.stderr and "Traceback" not in result.stderr


def test_default_report_and_explicit_body_option(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    result = run("--output", str(path))
    assert result.returncode == 0 and not result.stdout and not result.stderr
    report = json.loads(path.read_text())
    assert report["status"] == "PASS" and report["mode"] == "fixture"
    assert report["runs"] == 3 and len(report["summaries"]) == 18
    assert {s["category"] for s in report["summaries"]} == {
        "self_intro",
        "preferences",
        "world_lore",
    }
    assert "structure_checks" not in report and "entry_system_support" not in report
    assert len(report["commit"]["sha"]) == 40 and isinstance(report["commit"]["dirty"], bool)
    assert "responses" not in report and "Luma" not in path.read_text()
    assert report["gates"]["execution_incomplete"]["count"] == 0
    explicit = run("--include-responses")
    assert explicit.returncode == 0 and json.loads(explicit.stdout)["responses"]


def test_bad_input_error_hides_content_and_path(tmp_path: Path) -> None:
    path = tmp_path / "sensitive-marker.json"
    path.write_text('{"sensitive-marker": "not-a-case"}')
    result = run("--cases", str(path))
    assert result.returncode == 1 and not result.stdout
    assert "sensitive-marker" not in result.stderr


@pytest.mark.parametrize(
    "scenario,gate",
    [
        ("fail", None),
        ("persona_break", "persona_break"),
        ("card_hash_mismatch", "card_hash_mismatch"),
        ("card_hash_missing", "card_hash_missing"),
        ("execution_incomplete", "execution_incomplete"),
        ("mismatch", None),
    ],
)
def test_cli_failure_scenarios(scenario: str, gate: str | None) -> None:
    result = run("--scenario", scenario, "--mismatch-policy", "gate")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "FAIL"
    if gate:
        assert report["gates"][gate]["count"] > 0
    elif scenario == "fail":
        assert any(s["status"] == "FAIL" for s in report["summaries"])
    else:
        assert report["mismatch"]["count"] == 3


@pytest.mark.parametrize("policy,exit_code", [("warn", 0), ("gate", 1)])
def test_cli_mismatch_at_ninety_percent_is_independent_gate(
    tmp_path: Path, policy: str, exit_code: int
) -> None:
    cases = json.loads((ROOT / "evals/persona/synthetic.cases.json").read_text())
    fixture = json.loads((ROOT / "evals/persona/responses.fixture.json").read_text())
    intro = cases["cases"][0]
    for i in range(1, 10):
        copy = {
            **intro,
            "id": f"intro-{i}",
            "messages": [{"role": "user", "content": f"合成自己紹介 {i}"}],
        }
        cases["cases"].append(copy)
        fixture["responses"][copy["id"]] = fixture["responses"][intro["id"]]
    cases_path, fixture_path = tmp_path / "cases.json", tmp_path / "fixture.json"
    cases_path.write_text(json.dumps(cases))
    fixture_path.write_text(json.dumps(fixture))
    result = run(
        "--cases",
        str(cases_path),
        "--fixture",
        str(fixture_path),
        "--scenario",
        "mismatch",
        "--mismatch-policy",
        policy,
    )
    assert result.returncode == exit_code
    report = json.loads(result.stdout)
    assert report["mismatch"]["count"] == 3
    assert all(s["status"] == "PASS" for s in report["summaries"])
    assert all(g["count"] == 0 for g in report["gates"].values())


def test_fixed_miori_case_version_is_reported_with_its_card(tmp_path: Path) -> None:
    import hashlib

    from digital_souls_core.persona_evaluation import load_cases

    cases_path = ROOT / "evals/persona/miori.cases.json"
    cases = load_cases(cases_path)
    card_path = ROOT / "characters/miori/miori.card.json"
    characters = json.loads((ROOT / "evals/persona/synthetic.characters.json").read_text())
    characters[0].update(character_id="miori", alias="miori", card_path=str(card_path))
    character_path = tmp_path / "characters.json"
    character_path.write_text(json.dumps(characters))
    fixture_path = tmp_path / "responses.json"
    fixture_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "responses": {c.id: " ".join(g[0] for g in c.required) for c in cases.cases},
                "scenarios": {"pass": {}},
            }
        )
    )
    result = run(
        "--characters",
        str(character_path),
        "--character-id",
        "miori",
        "--cases",
        str(cases_path),
        "--fixture",
        str(fixture_path),
        "--runs",
        "3",
    )
    assert result.returncode == 0 and not result.stderr
    report = json.loads(result.stdout)
    assert report["case_version"] == cases.version and report["cases_draft"] is False
    assert report["status"] == "PASS" and report["quality_evidence"] is False
    assert len(report["cases"]) == 42
    assert {c["card_sha256"] for c in report["cases"]} == {
        hashlib.sha256(card_path.read_bytes()).hexdigest()
    }
