"""CLI failure/profile boundaries are in-process and never access a database."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools/evaluate-semantic-retrieval.py"
pytestmark = pytest.mark.it1


@pytest.mark.parametrize("argument", [["--runs", "0"], ["--runs", "-1"]])
def test_cli_rejects_invalid_run_count(argument: list[str]) -> None:
    process = subprocess.run(
        [sys.executable, str(CLI), *argument],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 1 and process.stdout == ""
    assert "検索評価に失敗" in process.stderr and "Traceback" not in process.stderr


def test_cli_profile_failure_hides_path_and_content_without_fallback(tmp_path: Path) -> None:
    profile = tmp_path / "sensitive-path-marker.json"
    profile.write_text('{"sensitive-content-marker": "invalid"}')
    process = subprocess.run(
        [sys.executable, str(CLI), "--profile", str(profile)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 1 and process.stdout == ""
    assert "sensitive" not in process.stderr and "Traceback" not in process.stderr


def test_cli_disabled_profile_is_rejected_before_any_storage(tmp_path: Path) -> None:
    profile = tmp_path / "disabled.json"
    profile.write_text((ROOT / "examples/embedding.example.json").read_text())
    process = subprocess.run(
        [sys.executable, str(CLI), "--profile", str(profile)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 1 and process.stdout == ""
    assert "検索評価に失敗" in process.stderr


def test_cli_bad_input_hides_body(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"private-text-marker": "invalid"}))
    process = subprocess.run(
        [sys.executable, str(CLI), "--cases", str(cases)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 1 and process.stdout == ""
    assert "private-text" not in process.stderr and "Traceback" not in process.stderr
