"""受入driverの再実行が元の会話イベントを重複・上書きしないことを検証する。"""

import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/acceptance_semantic_scenario.py"


@pytest.mark.parametrize("stopped_after_reply", [True, False], ids=["reply-stop", "poll-timeout"])
def test_recorded_label_without_snapshot_is_rejected_before_http(monkeypatch, stopped_after_reply):
    with TemporaryDirectory(prefix="ds-memory-341-", dir="/tmp") as directory:
        root = Path(directory)
        (root / "data").mkdir()
        (root / "runtime-manifest.json").write_text(json.dumps({
            "status": "ready", "environmentId": "test", "dataRoot": str(root / "data"),
            "backend": "http://127.0.0.1:9",
        }))
        original = json.dumps({"label": "prior", "conversation_id": "original"}) + "\n"
        events = root / "lifecycle-events.jsonl"
        events.write_text(original)
        if stopped_after_reply:
            (root / "stop").write_text("owned restart acceptance")
        monkeypatch.setenv("DS_ENVIRONMENT_ID", "test")
        monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(root), "prior", "新しい発言"])

        def unexpected_http(*args, **kwargs):
            pytest.fail("記録済みlabelでHTTP会話を開始してはいけない")

        monkeypatch.setattr(httpx, "Client", unexpected_http)
        with pytest.raises(RuntimeError, match="use a new label"):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        assert events.read_text() == original
        assert not (root / "lifecycle-prior.json").exists()


def test_new_label_can_reach_http_with_other_recorded_events(monkeypatch):
    with TemporaryDirectory(prefix="ds-memory-341-", dir="/tmp") as directory:
        root = Path(directory)
        (root / "data").mkdir()
        (root / "runtime-manifest.json").write_text(json.dumps({
            "status": "ready", "environmentId": "test", "dataRoot": str(root / "data"),
            "backend": "http://127.0.0.1:9",
        }))
        events = root / "lifecycle-events.jsonl"
        original = json.dumps({"label": "prior"}) + "\n"
        events.write_text(original)
        monkeypatch.setenv("DS_ENVIRONMENT_ID", "test")
        monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(root), "next", "新しい発言"])

        def reached_http(*args, **kwargs):
            raise RuntimeError("HTTP boundary reached")

        monkeypatch.setattr(httpx, "Client", reached_http)
        with pytest.raises(RuntimeError, match="HTTP boundary reached"):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        assert events.read_text() == original
