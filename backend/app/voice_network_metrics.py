"""応答に相関したブラウザ音声RTP統計を、本文や接続先なしで集計する。"""
from __future__ import annotations

from typing import Any

from app.voice_metrics import DiagnosticValue, NetworkCollection, NetworkMetadata


_MISSING_REASONS = {
    "stats_api_unavailable", "stats_failed", "stats_timeout", "stats_unavailable",
    "ambiguous_audio_stream", "invalid_rtp_counters", "counter_regressed",
    "playback_packets_not_yet_reported", "network_observation_absent",
}


def aggregate_network(trials: list[dict[str, Any]], *, condition: str) -> NetworkMetadata:
    byte_counts: dict[str, list[int]] = {"uplink": [], "downlink": []}
    received_packets = lost_packets = negative_loss_trials = loss_trials = 0
    missing: dict[str, int] = {}
    for trial in trials:
        observation = trial.get("network_observation")
        if observation is None:
            observation = {side: {"status": "missing", "reason": "network_observation_absent"}
                           for side in byte_counts}
        elif not isinstance(observation, dict) or observation.get("method") != "browser_audio_rtp_counters_v1":
            raise ValueError("invalid network observation method")
        for side in byte_counts:
            raw = observation.get(side)
            if not isinstance(raw, dict):
                raise ValueError("missing network direction")
            if raw.get("status") == "missing":
                reason = raw.get("reason")
                if reason not in _MISSING_REASONS:
                    raise ValueError("invalid network missing reason")
                key = f"{side}:{reason}"
                missing[key] = missing.get(key, 0) + 1
                continue
            if raw.get("status") != "measured" or any(
                type(raw.get(key)) is not int or not 0 <= raw[key] <= 2**53 - 1 for key in ("bytes", "packets")
            ):
                raise ValueError("invalid network counters")
            byte_counts[side].append(raw["bytes"])
            if side == "downlink":
                lost = raw.get("lostPackets")
                if type(lost) is not int or not -(2**53 - 1) <= lost <= 2**53 - 1 or raw["packets"] + lost < 0:
                    raise ValueError("invalid network loss counter")
                completion = trial.get("playback_completion")
                if isinstance(completion, dict) and raw["packets"] < completion["packetCount"]:
                    raise ValueError("network counters precede confirmed playback")
                received_packets += raw["packets"]
                # 負の値を別streamの損失と相殺させず、件数として残す。
                lost_packets += max(0, lost)
                negative_loss_trials += int(lost < 0)
                loss_trials += 1
    def bytes_value(side: str) -> DiagnosticValue:
        if trials and len(byte_counts[side]) == len(trials):
            return DiagnosticValue(status="measured", value=sum(byte_counts[side]))
        return DiagnosticValue(status="missing", reason=f"{side}_counter_coverage_incomplete")
    denominator = received_packets + lost_packets
    loss = DiagnosticValue(status="missing", reason="downlink_loss_coverage_incomplete")
    if trials and loss_trials == len(trials) and denominator > 0:
        loss = DiagnosticValue(status="measured", value=lost_packets * 10_000 / denominator)
    return NetworkMetadata(
        sent_bytes=bytes_value("uplink"), received_bytes=bytes_value("downlink"),
        packet_loss_basis_points=loss, condition=condition,
        collection=NetworkCollection(trial_count=len(trials), sent_trials=len(byte_counts["uplink"]),
                                     received_trials=len(byte_counts["downlink"]), loss_trials=loss_trials,
                                     received_packets=received_packets, lost_packets=lost_packets,
                                     negative_loss_trials=negative_loss_trials, missing_trials=missing),
    )
