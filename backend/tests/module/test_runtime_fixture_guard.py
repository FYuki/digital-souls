from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("environment_id", ["dogfood", "test"])
def test_rt_clean_01_pytest_fixture_refuses_dogfood_marker_without_touching_data(
    tmp_path: Path,
    environment_id: str,
) -> None:
    data_root = tmp_path / "dogfood-data"
    data_root.mkdir()
    marker = {"schemaVersion": 1, "environmentId": "dogfood"}
    marker_path = data_root / ".environment-identity.json"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    sqlite_path = data_root / "conversation-history.db"
    sqlite_path.write_bytes(b"dogfood-history")
    chroma_sentinel = data_root / "chroma" / "sentinel"
    chroma_sentinel.parent.mkdir()
    chroma_sentinel.write_text("dogfood-memory", encoding="utf-8")
    cache_sentinel = data_root / "cache" / "sentinel"
    cache_sentinel.parent.mkdir()
    cache_sentinel.write_text("dogfood-cache", encoding="utf-8")
    environment = {
        **os.environ,
        "DS_ENVIRONMENT_ID": environment_id,
        "DS_DATA_DIR": str(data_root),
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "backend/tests/unit/test_audio_constants.py",
        ],
        cwd=ROOT_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert json.loads(marker_path.read_text(encoding="utf-8")) == marker
    assert sqlite_path.read_bytes() == b"dogfood-history"
    assert chroma_sentinel.read_text(encoding="utf-8") == "dogfood-memory"
    assert cache_sentinel.read_text(encoding="utf-8") == "dogfood-cache"


def test_rt_clean_01_pytest_fixture_accepts_markerless_test_input(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "test-data"
    data_root.mkdir()
    environment = {
        **os.environ,
        "DS_ENVIRONMENT_ID": "test",
        "DS_DATA_DIR": str(data_root),
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "backend/tests/unit/test_audio_constants.py",
        ],
        cwd=ROOT_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert list(data_root.iterdir()) == []
