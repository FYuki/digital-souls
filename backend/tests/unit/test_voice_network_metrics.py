from __future__ import annotations

import pytest
from app.voice_network_metrics import aggregate_network


def trial(*, lost=2):
    return {"network_observation": {"method": "browser_audio_rtp_counters_v1",
        "uplink": {"status": "measured", "bytes": 100, "packets": 10},
        "downlink": {"status": "measured", "bytes": 200, "packets": 18, "lostPackets": lost}},
        "playback_completion": {"packetCount": 18}}


def test_network_uses_measured_trial_bytes_and_downlink_loss_denominator():
    value = aggregate_network([trial(), trial(lost=0)], condition="controlled")
    assert value.sent_bytes.value == 200
    assert value.received_bytes.value == 400
    assert value.packet_loss_basis_points.value == pytest.approx(2 * 10000 / 38)
    assert value.collection.loss_scope == "browser_downlink_response_tracks"
    assert value.collection.received_packets == 36


def test_negative_loss_does_not_cancel_another_stream_loss():
    value = aggregate_network([trial(), trial(lost=-2)], condition="controlled")
    assert value.collection.lost_packets == 2
    assert value.collection.negative_loss_trials == 1


def test_network_missing_counters_are_not_replaced_by_zero():
    value = aggregate_network([trial(), {}], condition="controlled")
    assert value.sent_bytes.status == "missing"
    assert value.received_bytes.status == "missing"
    assert value.packet_loss_basis_points.status == "missing"
    assert value.collection.trial_count == 2
    assert value.collection.sent_trials == 1
    assert value.collection.missing_trials == {"uplink:network_observation_absent": 1, "downlink:network_observation_absent": 1}


@pytest.mark.parametrize("change", ["bytes", "loss", "packets", "reason"])
def test_network_rejects_invalid_or_inconsistent_observations(change):
    item = trial()
    raw = item["network_observation"]["downlink"]
    if change == "bytes": raw["bytes"] = float("nan")
    if change == "loss": raw["lostPackets"] = -100
    if change == "packets": raw["packets"] = 17
    if change == "reason": raw.update(status="missing", reason="private-error")
    with pytest.raises(ValueError):
        aggregate_network([item], condition="controlled")
