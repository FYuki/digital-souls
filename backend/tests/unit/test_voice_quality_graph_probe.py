"""Graph診断の未capture・部分fallbackを成功扱いしない。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "scripts/voice_quality/probe_irodori_cuda_graph.py"
spec = importlib.util.spec_from_file_location("voice_quality_graph_probe_test", SOURCE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize("statistics", [
    [], [{"calls": 40, "captures": 0}], [{"calls": 40}],
    [{"captures": True}], [{"captures": 1.5}],
    [{"captures": 2}, {"captures": 0}],
])
def test_graph_request_without_verified_capture_is_fallback(statistics):
    assert probe.graph_trial_status(statistics) == "fallback"


def test_captured_requests_can_succeed():
    assert probe.graph_trial_status([{"calls": 40, "captures": 2}]) == "success"


def test_one_fallback_or_missing_trial_prevents_run_success():
    captured = {"phase": "cuda_graph", "status": "success"}
    assert probe.graph_run_status([{"phase": "baseline"}, captured]) == "success"
    for invalid in [{"phase": "cuda_graph", "status": "fallback"}, {"phase": "cuda_graph"}]:
        assert probe.graph_run_status([captured, invalid]) == "fallback"
    assert probe.graph_run_status([{"phase": "baseline"}]) == "fallback"
