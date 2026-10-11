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
    assert result.returncode == 1 and not result.stdout and not result.stderr
    report = json.loads(path.read_text())
    assert report["status"] == "FAIL" and report["mode"] == "fixture"
    assert len(report["commit"]["sha"]) == 40 and isinstance(report["commit"]["dirty"], bool)
    assert "responses" not in report and "Luma" not in path.read_text()
    assert report["gates"]["execution_incomplete"]["count"] == 3
    explicit = run("--include-responses")
    assert explicit.returncode == 1 and json.loads(explicit.stdout)["responses"]


def test_bad_input_error_hides_content_and_path(tmp_path: Path) -> None:
    path = tmp_path / "sensitive-marker.json"
    path.write_text('{"sensitive-marker": "not-a-case"}')
    result = run("--cases", str(path))
    assert result.returncode == 1 and not result.stdout
    assert "sensitive-marker" not in result.stderr
