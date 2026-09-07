from __future__ import annotations
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("server_clock", Path(__file__).resolve().parents[3] / "scripts/voice_quality/server_clock.py")
assert spec and spec.loader
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)

def probe(sent, received, server_received, server_sent, **extra):
    return dict(status="received", generation=1, sentAtMs=sent, receivedAtMs=received,
                serverReceivedAtUs=server_received, serverSentAtUs=server_sent, **extra)

def test_causal_bounds_do_not_assume_symmetric_network_or_shared_clock_origin():
    rows = [probe(100, 119, 5_000_000, 5_000_001), probe(120, 150, 5_000_020, 5_000_025)]
    result = clock.bound_transition(rows, 5_000_010_100, 5_000_011_200, 1)
    assert result["lower_ms"] == pytest.approx(99.8)
    assert result["upper_ms"] == pytest.approx(150.2)
    assert result["width_ms"] == pytest.approx(50.4)
    assert result["missing_reason"] is None

def test_sub_microsecond_floor_does_not_make_an_ambiguous_probe_a_before_anchor():
    rows = [probe(100, 110, 5000, 5001), probe(111, 115, 5003, 5004)]
    result = clock.bound_transition(rows, 5_001_500, 5_002_000, 1)
    assert result["lower_ms"] is None
    assert result["missing_reason"] == "server_transition_not_bracketed"

def test_missing_timeout_and_old_generation_are_counted_not_filled():
    rows = [dict(status="timeout", generation=1), dict(status="received", generation=1),
            dict(status="received", generation=0)]
    result = clock.bound_transition(rows, 1, 2, 1)
    assert result["missing_probes"] == 3
    assert result["width_ms"] is None

@pytest.mark.parametrize("bad", [float("nan"), -1, True, 1.5, 9007199254740992])
def test_invalid_server_clock_rejects_whole_calculation(bad):
    with pytest.raises(ValueError, match="invalid clock probe"):
        clock.bound_transition([probe(100, 101, bad, 9999999999999999)], 1, 2, 1)

def test_contradictory_clock_order_cannot_be_a_zero_width_success():
    with pytest.raises(ValueError, match="contradictory"):
        clock.bound_transition([probe(200, 210, 1, 2), probe(100, 110, 4, 5)], 3000, 3001, 1)
