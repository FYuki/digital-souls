"""応答に相関したブラウザ音声RTP統計を、本文や接続先なしで集計する。"""
from __future__ import annotations

from typing import Any, Literal

from app.voice_metrics import DiagnosticValue, NetworkCollection, NetworkMetadata, TraceEvent


_MISSING_REASONS = {
    "stats_api_unavailable", "stats_failed", "stats_timeout", "stats_unavailable",
    "ambiguous_audio_stream", "invalid_rtp_counters", "counter_regressed",
    "playback_packets_not_yet_reported", "network_observation_absent",
}


def aggregate_network(trials: list[dict[str, Any]], *, condition: str) -> NetworkMetadata:
    byte_counts: dict[str, list[int]] = {"uplink": [], "downlink": []}
    received_packets = lost_packets = negative_loss_trials = loss_trials = 0
    missing: dict[str, int] = {}
    boundaries: dict[Literal["playback_completed", "response_cancelled"], int] = {}
    for trial in trials:
        observation = trial.get("network_observation")
        if observation is None:
            observation = {side: {"status": "missing", "reason": "network_observation_absent"}
                           for side in byte_counts}
        elif not isinstance(observation, dict) or observation.get("method") != "browser_audio_rtp_counters_v1":
            raise ValueError("invalid network observation method")
        boundary = observation.get("boundary", "playback_completed")
        if boundary not in {"playback_completed", "response_cancelled"}:
            raise ValueError("invalid network observation boundary")
        if trial.get("network_observation") is not None:
            boundaries[boundary] = boundaries.get(boundary, 0) + 1
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
                                     negative_loss_trials=negative_loss_trials, missing_trials=missing, observation_boundaries=boundaries),
    )


_NETWORK_PREFIX = "network_rtp_"
_NETWORK_MARKER = "network_rtp_summary_recorded"


def network_summary_values(raw: object, *, expected_packets: int) -> dict[str, float]:
    """1応答のRTP snapshotを検証し、本文を含まないtrace値へ変換する。"""
    if (type(expected_packets) is not int or expected_packets < 0 or not isinstance(raw, dict)
            or set(raw) not in ({"method", "uplink", "downlink"}, {"method", "uplink", "downlink", "boundary"})
            or ("boundary" in raw and raw["boundary"] != "response_cancelled")):
        raise ValueError("invalid network summary")
    aggregate_network([{"network_observation": raw,
                        "playback_completion": {"packetCount": expected_packets}}], condition="native-summary-validation")
    values: dict[str, float] = {}
    for side in ("uplink", "downlink"):
        direction = raw[side]
        if not isinstance(direction, dict):
            raise ValueError("invalid network direction")
        if direction.get("status") == "missing":
            if set(direction) != {"status", "reason"} or direction.get("reason") not in _MISSING_REASONS:
                raise ValueError("invalid network missing reason")
            values[f"{_NETWORK_PREFIX}{side}_missing_{direction['reason']}"] = 1
        else:
            fields = {"bytes", "packets"} | ({"lostPackets"} if side == "downlink" else set())
            if set(direction) != {"status", *fields}:
                raise ValueError("invalid network counter fields")
            for field in fields:
                name = "lost_packets" if field == "lostPackets" else field
                # 範囲・負のloss・bool・完全再生までのpacket数は共通集計で検証する。
                value = direction[field]
                if type(value) is not int:
                    raise ValueError("network counters require integers")
                values[f"{_NETWORK_PREFIX}{side}_{name}"] = float(value)
    if raw.get("boundary") == "response_cancelled":
        values["network_rtp_cancelled_snapshot"] = 1
    values[_NETWORK_MARKER] = 1
    return values


def network_observation_from_trace(events: list["TraceEvent"]) -> dict[str, Any] | None:
    """同じ応答のtraceを再検証する。複数発話に配られた同一snapshotは一度だけ使う。"""
    selected = [event for event in events if event.name.startswith(_NETWORK_PREFIX)]
    if not selected:
        return None
    if len({(event.session_id, event.response_id) for event in selected}) != 1:
        raise ValueError("network trace must belong to one response")
    values: dict[str, float] = {}
    seen: set[tuple[str, str]] = set()
    timestamps = set()
    for event in selected:
        key = event.name, event.utterance_id
        if (key in seen or event.stage != "network" or event.outcome != "success"
                or event.value is None or event.clock_domain != "client_monotonic" or event.unit != "millisecond"):
            raise ValueError("invalid or duplicate native network trace")
        seen.add(key)
        timestamps.add(event.timestamp)
        if event.name in values and values[event.name] != event.value:
            raise ValueError("conflicting network snapshots for response")
        values[event.name] = event.value
    if len(timestamps) != 1 or values.get(_NETWORK_MARKER) != 1:
        raise ValueError("incomplete native network snapshot")
    observation: dict[str, Any] = {"method": "browser_audio_rtp_counters_v1"}
    if values.get("network_rtp_cancelled_snapshot") == 1:
        observation["boundary"] = "response_cancelled"
    for side in ("uplink", "downlink"):
        prefix = f"{_NETWORK_PREFIX}{side}_"
        missing = [name.removeprefix(prefix + "missing_") for name in values if name.startswith(prefix + "missing_")]
        if missing:
            if len(missing) != 1:
                raise ValueError("conflicting network missing reasons")
            observation[side] = {"status": "missing", "reason": missing[0]}
        else:
            fields = {"bytes": "bytes", "packets": "packets"}
            if side == "downlink":
                fields["lostPackets"] = "lost_packets"
            direction: dict[str, Any] = {"status": "measured"}
            for field, suffix in fields.items():
                value = values.get(prefix + suffix)
                if value is None or not value.is_integer():
                    raise ValueError("missing native network counter")
                direction[field] = int(value)
            observation[side] = direction
    if network_summary_values(observation, expected_packets=0) != values:
        raise ValueError("native network snapshot has extra or conflicting values")
    return observation


def aggregate_network_trace(events: list["TraceEvent"], *, condition: str) -> NetworkMetadata:
    """応答単位で送受信量を合計する。割り込み専用trialを追加の送受信と数えない。"""
    trials: dict[tuple[str, str, str], list[TraceEvent]] = {}
    for event in events:
        trials.setdefault((event.session_id, event.utterance_id, event.response_id), []).append(event)
    responses: dict[tuple[str, str], list[TraceEvent]] = {}
    for trial in trials.values():
        if any(event.name == "interruption_started" for event in trial):
            continue
        responses.setdefault((trial[0].session_id, trial[0].response_id), []).extend(trial)
    rows = [{"network_observation": network_observation_from_trace(response)} for response in responses.values()]
    return aggregate_network(rows, condition=condition)
