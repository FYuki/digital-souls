from __future__ import annotations

import argparse
import json
import math
import hashlib
import platform
from pathlib import Path
from typing import Sequence
from uuid import UUID

from jsonschema import Draft202012Validator

from app.stt.remote_whisper_client import WHISPER_COMPUTE_TYPE, WHISPER_DEVICE
from app.voice_baseline import _assert_anonymous, _load_manifest, _load_trace
from app.voice_resource_metrics import aggregate_resources
from app.voice_network_metrics import aggregate_network
from app.voice_metrics import (
    ClockMetadata, DiagnosticValue, HardwareMetadata,
    ResourceMetadata, RunDiagnostics, TraceEvent, aggregate_events, create_run_metadata,
)


def _finalize_livekit_report(
    *, manifest_path: Path, trace_path: Path, output_path: Path,
    schema_path: Path, profile_report_path: Path, run_id: str, controlled: bool,
    resource_observations_path: Path | None = None,
) -> None:
    """試行数と証拠を検証する。品質合否は個別のevaluatorで判定する。"""
    manifest = _load_manifest(manifest_path)
    profile = json.loads(profile_report_path.read_text())
    if profile.get("effectiveProfile") != "integration-voice":
        raise ValueError("pilot requires the integration-voice profile")
    configured = profile.get("derivedEnvironment", {})
    if controlled and configured.get("RAG_ENABLED") != "false":
        raise ValueError("controlled evidence requires the resolved RAG-disabled profile")
    whisper_model = configured.get("WHISPER_MODEL")
    if not isinstance(whisper_model, str) or not whisper_model:
        raise ValueError("pilot requires the resolved Whisper model")
    scope = "controlled" if controlled else "pilot"
    warmup_count = 5 if controlled else 1
    if manifest.get("measurement_scope") != scope:
        raise ValueError("report scope must match the manifest")
    trials = manifest.get("trials")
    expected = manifest.get("expected_measured")
    if type(expected) is not int or (expected != 100 if controlled else not 1 <= expected < 100):
        raise ValueError("controlled requires 100 measured trials; pilot requires 1 to 99")
    if not isinstance(trials, list) or len(trials) != expected + warmup_count:
        raise ValueError("report requires all warmup and measured trials")
    if manifest.get("expected_warmup") != warmup_count:
        raise ValueError("incorrect warmup count")
    identities: dict[str, list[str]] = {key: [] for key in (
        "sessionId", "utteranceId", "responseId", "conversationId",
    )}
    fixture = manifest.get("fixture")
    if not isinstance(fixture, dict):
        raise ValueError("pilot fixture metadata is required")
    fixture_root = Path(__file__).resolve().parents[2] / "frontend/playwright/fixtures"
    reference = json.loads((fixture_root / "speech.metadata.json").read_text())
    if hashlib.sha256((fixture_root / "speech.wav").read_bytes()).hexdigest() != reference["audio_sha256"]:
        raise ValueError("reference fixture hash mismatch")
    if any(fixture.get(key) != reference[key] for key in (
        "fixture_version", "audio_sha256", "sample_rate_hz", "speech_start_sample", "speech_end_sample",
    )):
        raise ValueError("pilot fixture does not match the repository fixture")
    if not isinstance(manifest.get("initial_state_hash"), str) or not manifest["initial_state_hash"]:
        raise ValueError("pilot requested initial state hash is required")
    sample_rate = fixture.get("sample_rate_hz")
    start_sample = fixture.get("speech_start_sample")
    end_sample = fixture.get("speech_end_sample")
    if (
        type(sample_rate) is not int or sample_rate <= 0
        or type(start_sample) is not int or type(end_sample) is not int
        or not 0 <= start_sample < end_sample
    ):
        raise ValueError("invalid fixture boundaries")
    for index, trial in enumerate(trials):
        if not isinstance(trial, dict) or trial.get("outcome") == "failure":
            raise ValueError("failed pilot trials cannot be finalized as successful observations")
        if trial.get("phase") != ("warmup" if index < warmup_count else "measured"):
            raise ValueError("pilot phases must be one warmup followed by measured trials")
        if trial.get("transcript_matches") is not True:
            raise ValueError("pilot transcript must match the fixed fixture")
        for key in ("fixture_version", "audio_sha256"):
            if trial.get(key) != fixture.get(key):
                raise ValueError("pilot fixture identity mismatch")
        if trial.get("initial_state_hash") != manifest.get("initial_state_hash"):
            raise ValueError("pilot requested initial state mismatch")
        if controlled:
            if trial.get("outcome") != "success" or trial.get("first_playback_method") != "audio_worklet_output_timestamp":
                raise ValueError("controlled trial requires explicit success and playback method")
            if trial.get("session_end_confirmed") is not True:
                raise ValueError("controlled trial requires confirmed session cleanup")
            _validate_initial_state_evidence(trial)
        for key, values in identities.items():
            value = trial.get(key)
            if not isinstance(value, str):
                raise ValueError("pilot correlation is missing")
            UUID(value)
            values.append(value)
    if any(len(set(values)) != len(values) for values in identities.values()):
        raise ValueError("pilot trials must use independent identities")
    measured = trials[warmup_count:]
    keys = {(trial["sessionId"], trial["utteranceId"], trial["responseId"]) for trial in trials}
    events = [event for event in _load_trace(trace_path) if (
        event.session_id, event.utterance_id, event.response_id,
    ) in keys]
    if any(event.measurement_kind != "controlled_baseline" for event in events):
        raise ValueError("measurement kinds must not be mixed")
    for index, trial in enumerate(trials):
        pair = (trial["sessionId"], trial["utteranceId"], trial["responseId"])
        matched = [event for event in events if (event.session_id, event.utterance_id, event.response_id) == pair]
        if not matched:
            raise ValueError("pilot trace is missing a measured trial")
        if any(event.outcome != "success" for event in matched):
            raise ValueError("pilot contains failed or excluded processing")
        points = {event.name: event for event in matched}
        if not {"stt_completed", "llm_completed", "tts_pipeline_completed", "first_playback"} <= points.keys():
            raise ValueError("pilot lacks successful generation or playback observations")
        origin = trial["fixtureStartedAt"]
        playback = trial["startedAt"]
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in (origin, playback, trial["fixture_speech_end_client_ms"])):
            raise ValueError("pilot client timestamps must be finite non-negative numbers")
        point = points["first_playback"]
        if point.clock_domain != "client_monotonic" or point.unit != "millisecond" or abs(point.timestamp - playback) >= 1.1:
            raise ValueError("pilot playback timestamp does not match its trace")
        if controlled or trial.get("media_observation_method") == "response_track_stateful_opus_worklet_output":
            if trial.get("media_observation_method") != "response_track_stateful_opus_worklet_output":
                raise ValueError("controlled trial requires independent media observations")
            media_times = []
            for key, name in (("trackReceivedAt", "client_track_received"),
                              ("audioReceivedAt", "client_audio_received"),
                              ("audioDecodeAt", "client_audio_decoded")):
                value = trial.get(key)
                observed = points.get(name)
                if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                    or observed is None or observed.clock_domain != "client_monotonic"
                    or observed.unit != "millisecond" or abs(observed.timestamp - value) >= 1.1):
                    raise ValueError("controlled media timestamp is missing or inconsistent")
                media_times.append(value)
            if not media_times[0] <= media_times[1] <= media_times[2] <= playback:
                raise ValueError("controlled media boundaries are out of order")
            validate_packet_playback_observation(trial)
            media = trial["track_media_observation"]
            for value, field in zip(media_times, ("trackReceivedAtMs", "firstPacketReceivedAtMs", "firstPacketDecodedAtMs")):
                if not isinstance(media, dict) or media.get(field) != value:
                    raise ValueError("media boundaries do not match the played packet")
        if trial.get("packet_playback_observation") is not None:
            validate_packet_playback_observation(trial)
        if trial.get("playback_completion") is not None:
            completion = validate_playback_completion(trial, points)
            raw_completion = trial["playback_completion"]
            # 先頭出力frameから無欠損で連続再生した末尾と、実際の末尾を同じ時計で比較する。
            for name, frame in (
                ("scheduled_playout", raw_completion["firstOutputFrame"] + raw_completion["expectedSamples"]),
                ("frame_playout", raw_completion["lastOutputEndFrame"]),
            ):
                events.append(TraceEvent(
                    schema_version="1.0", measurement_kind="controlled_baseline",
                    event_id=f"{name}-{index}", character_id=matched[0].character_id,
                    session_id=pair[0], utterance_id=pair[1], response_id=pair[2],
                    name=name, stage="playback", outcome="success", timestamp=frame / 48,
                    clock_domain="browser_audio_context", unit="millisecond",
                ))
            for name, value in completion.items():
                events.append(TraceEvent(
                    schema_version="1.0", measurement_kind="controlled_baseline",
                    event_id=f"{name}-{index}", character_id=matched[0].character_id,
                    session_id=pair[0], utterance_id=pair[1], response_id=pair[2],
                    name=name, stage="playback", outcome="success",
                    timestamp=float(trial["playback_completion"]["confirmationObservedAtMs"]),
                    clock_domain="client_monotonic", unit="millisecond", value=value,
                ))
        fixture_start, fixture_end = _validate_fixture_clock(
            trial, sample_rate=sample_rate, start_sample=start_sample, end_sample=end_sample,
            controlled=controlled,
        )
        for name, timestamp in (("fixture_speech_start", fixture_start), ("fixture_speech_end", fixture_end)):
            events.append(TraceEvent(
                schema_version="1.0", measurement_kind="controlled_baseline",
                event_id=f"{name}-{index}", character_id=matched[0].character_id,
                session_id=pair[0], utterance_id=pair[1], response_id=pair[2],
                name=name, stage="fixture", outcome="success",
                timestamp=timestamp,
                clock_domain="client_monotonic", unit="millisecond",
            ))
    measured_keys = {(trial["sessionId"], trial["utteranceId"], trial["responseId"]) for trial in measured}
    events = [event for event in events if (event.session_id, event.utterance_id, event.response_id) in measured_keys]
    missing_resource = DiagnosticValue(status="missing", reason="process_tree_not_observed")
    resources = ResourceMetadata(cpu_percent=missing_resource, memory_bytes=missing_resource)
    if resource_observations_path is not None:
        rows = [json.loads(line) for line in resource_observations_path.read_text().splitlines() if line.strip()]
        if not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError("resource observations must contain sampled rows")
        resources = aggregate_resources(rows)
    artifact = aggregate_events(
        events,
        metadata=create_run_metadata(
            measurement_kind="controlled_baseline", transport="livekit", run_id=run_id,
            profile="integration-voice", fixture_version=fixture["fixture_version"],
            warmup_runs=warmup_count, measured_runs=expected, whisper_model=whisper_model,
            whisper_device=WHISPER_DEVICE, whisper_compute_type=WHISPER_COMPUTE_TYPE,
        ),
        diagnostics=RunDiagnostics(
            clocks=[
                ClockMetadata(domain="server_monotonic", method="perf_counter_ns", unit="nanosecond"),
                ClockMetadata(domain="client_monotonic", method="performance_now_and_audio_output_timestamp", unit="millisecond"),
                *([ClockMetadata(domain="browser_audio_context", method="confirmed_output_frame_divided_by_sample_rate", unit="millisecond")]
                  if any(trial.get("playback_completion") is not None for trial in measured) else []),
            ],
            hardware=HardwareMetadata(description=platform.platform()),
            resources=resources,
            network=aggregate_network(measured, condition=f"localhost LiveKit {scope}; shared inference services; RAG disabled; browser audio RTP payload; loss at browser downlink"),
        ),
    )
    if artifact.run_counts.measured != expected:
        raise ValueError("pilot trace has unexpected trial identities")
    serialized = artifact.model_dump(mode="json")
    _assert_anonymous(serialized)
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(serialized)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2) + "\n")



def validate_playback_completion(
    trial: dict[str, object], points: dict[str, TraceEvent],
) -> dict[str, float]:
    """送信総数・連続RTP・出力時計を照合する。音切れがあっても計測値として残す。"""
    raw = trial.get("playback_completion")
    if not isinstance(raw, dict):
        raise ValueError("playback completion is unavailable")
    counts = {}
    for name in (
        "expectedSamples", "inputSamples", "paddingSamples", "renderedSamples", "packetCount",
        "firstOutputFrame", "lastOutputEndFrame", "gapSamples", "maximumGapSamples", "gapCount",
        "firstRtpTimestamp", "lastRtpTimestamp", "sampleRate",
    ):
        value = raw.get(name)
        if type(value) is not int or not 0 <= value <= 2**53 - 1:
            raise ValueError("invalid completion sample count")
        counts[name] = value
    expected, gap, packets = counts["expectedSamples"], counts["gapSamples"], counts["packetCount"]
    if (counts["sampleRate"] != 48000 or not expected or expected != packets * 960
        or counts["renderedSamples"] != expected
        or counts["inputSamples"] + counts["paddingSamples"] != expected
        or counts["paddingSamples"] > 1919
        or counts["lastOutputEndFrame"] - counts["firstOutputFrame"] != expected + gap
        or not 0 <= counts["maximumGapSamples"] <= gap
        or (gap == 0) != (counts["gapCount"] == 0)
        or (gap == 0) != (counts["maximumGapSamples"] == 0)
        or counts["gapCount"] > gap
        or gap > counts["gapCount"] * counts["maximumGapSamples"]):
        raise ValueError("completion sample conservation mismatch")
    if (counts["firstRtpTimestamp"] > 0xffffffff or counts["lastRtpTimestamp"] > 0xffffffff
        or (counts["lastRtpTimestamp"] - counts["firstRtpTimestamp"]) % 2**32 != ((packets - 1) * 960) % 2**32):
        raise ValueError("completion RTP timeline mismatch")
    for field, name in (("inputSamples", "response_audio_input_samples"),
                        ("expectedSamples", "response_audio_captured_samples"),
                        ("paddingSamples", "response_audio_padding_samples")):
        point = points.get(name)
        if point is None or point.outcome != "success" or point.value != counts[field]:
            raise ValueError("completion does not match source trace")
    times: list[float] = []
    for name in ("outputClockContextTime", "outputClockPerformanceTime", "confirmationObservedAtMs"):
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid completion output clock")
        times.append(value)
    context, performance, observed = times
    end_at = performance + (counts["lastOutputEndFrame"] / 48000 - context) * 1000
    first = trial.get("packet_playback_observation")
    if (context * 48000 < counts["lastOutputEndFrame"] or not 0 <= end_at <= observed
        or not isinstance(first, dict) or first.get("firstOutputFrame") != counts["firstOutputFrame"]):
        raise ValueError("completion output clock has not confirmed the response")
    return {"playback_gap_total_ms": gap / 48,
            "playback_gap_maximum_ms": counts["maximumGapSamples"] / 48,
            "playback_underrun_count": float(counts["gapCount"]),
            "playback_duration_ms": (expected + gap) / 48}


def validate_packet_playback_observation(trial: dict[str, object]) -> tuple[float, float]:
    """同一packetの出力時計通過を再検証する。source PCM offsetや品質合格は示さない。"""
    evidence = trial.get("packet_playback_observation")
    media = trial.get("track_media_observation")
    if not isinstance(evidence, dict) or not isinstance(media, dict):
        raise ValueError("packet playback evidence is unavailable")

    def number(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid packet playback number")
        return float(value)

    if evidence.get("packetIndex") != 0 or type(evidence.get("packetIndex")) is not int:
        raise ValueError("first playback packet is not zero")
    if evidence.get("outputClockPassed") is not True or evidence.get("sourcePcmOffsetVerified") is not False:
        raise ValueError("unsupported packet playback claim")
    if evidence.get("sampleRate") != 48000 or media.get("firstPacketDecodedSamples") != 960:
        raise ValueError("packet playback format mismatch")
    if not trial.get("track_response_matches") or media.get("packetDecodeMissingReason") is not None:
        raise ValueError("packet response correlation unavailable")
    received, decoded, played, observed = (number(evidence.get(name)) for name in (
        "receivedAtMs", "decodedAtMs", "firstOutputAtMs", "confirmationObservedAtMs",
    ))
    if not received <= decoded <= played <= observed:
        raise ValueError("packet playback clock order mismatch")
    for name, value in (("firstPacketReceivedAtMs", received), ("firstPacketDecodedAtMs", decoded)):
        if abs(number(media.get(name)) - value) > 0.001:
            raise ValueError("packet playback does not match the observed packet")
    first, end = number(evidence.get("firstOutputFrame")), number(evidence.get("firstOutputEndFrame"))
    if evidence.get("renderClockMethod") is not None:
        if evidence["renderClockMethod"] != "quantum_count_reconciled_with_global_frame":
            raise ValueError("unsupported render clock method")
        quantum = number(evidence.get("renderQuantumStartFrame"))
        anchor = number(evidence.get("renderClockConfirmationFrame"))
        if not quantum.is_integer() or not anchor.is_integer() or not quantum <= first < end <= quantum + 128 or anchor < quantum:
            raise ValueError("render quantum clock is not reconciled")
    context_time = number(evidence.get("outputClockContextTime"))
    performance_time = number(evidence.get("outputClockPerformanceTime"))
    if not first.is_integer() or not end.is_integer() or not 0 < end - first <= 960 or context_time * 48000 < end:
        raise ValueError("output clock has not passed the first packet samples")
    mapped = performance_time + (first / 48000 - context_time) * 1000
    if abs(mapped - played) > 0.001 or abs(number(trial.get("startedAt")) - played) > 0.001:
        raise ValueError("first playback does not match the output clock")
    return decoded - received, played - decoded


def _validate_fixture_clock(
    trial: dict[str, object], *, sample_rate: int, start_sample: int, end_sample: int,
    controlled: bool,
) -> tuple[float, float]:
    """PCM出力の因果境界を検証する。旧入力時計はpilotの履歴解析だけに許可する。"""
    def timestamp(value: object) -> float:
        if type(value) not in (int, float) or not isinstance(value, (int, float)):
            raise ValueError("fixture clock requires numeric timestamps")
        if not math.isfinite(value) or value < 0:
            raise ValueError("fixture clock requires finite non-negative timestamps")
        return float(value)

    origin = timestamp(trial.get("fixtureStartedAt"))
    recorded_end = timestamp(trial.get("fixture_speech_end_client_ms"))
    method = trial.get("fixture_clock_method")
    if method != "audio_worklet_pcm_causal_bounds":
        if controlled:
            raise ValueError("controlled fixture requires observed PCM causal bounds")
        if method not in (None, "get_user_media_completion_unverified") or "fixture_clock_bounds" in trial:
            raise ValueError("unknown or inconsistent fixture clock method")
        expected_end = origin + end_sample * 1000 / sample_rate
        if abs(recorded_end - expected_end) > 0.001:
            raise ValueError("fixture end does not match sample boundaries")
        return origin + start_sample * 1000 / sample_rate, expected_end

    maximum = timestamp(trial.get("fixture_clock_maximum_uncertainty_ms"))
    if not 0 < maximum <= 20:
        raise ValueError("fixture clock uncertainty limit must be at most 20 ms")
    bounds = trial.get("fixture_clock_bounds")
    if not isinstance(bounds, dict) or set(bounds) != {"sourceStart", "speechStart", "speechEnd"}:
        raise ValueError("fixture clock requires all three PCM boundaries")
    lower_values: list[float] = []
    upper_values: list[float] = []
    for name, sample in (("sourceStart", 0), ("speechStart", start_sample), ("speechEnd", end_sample)):
        boundary = bounds[name]
        if not isinstance(boundary, dict) or set(boundary) != {"lowerMs", "upperMs", "sourceSample"}:
            raise ValueError("fixture clock boundary is incomplete")
        if type(boundary["sourceSample"]) is not int or boundary["sourceSample"] != sample:
            raise ValueError("fixture clock boundary does not match its source sample")
        lower, upper = timestamp(boundary["lowerMs"]), timestamp(boundary["upperMs"])
        if upper < lower or upper - lower > maximum:
            raise ValueError("fixture clock boundary is reversed or too uncertain")
        lower_values.append(lower)
        upper_values.append(upper)
    if lower_values != sorted(lower_values) or upper_values != sorted(upper_values):
        raise ValueError("fixture clock boundaries are out of order")
    if abs(origin - lower_values[0]) > 0.001 or abs(recorded_end - lower_values[2]) > 0.001:
        raise ValueError("fixture timestamps do not match observed causal lower bounds")
    # 発話末尾の下限を使い、時計の不確かさで遅延を過小評価しない。
    return lower_values[1], lower_values[2]


def _validate_initial_state_evidence(trial: dict[str, object]) -> None:
    from app.memory.persistence.schema import PERSONA_MEMORY_TABLES

    evidence = trial.get("initial_state_evidence")
    if not isinstance(evidence, dict) or evidence.get("method") != "sqlite_empty_state_and_configuration_v1":
        raise ValueError("controlled trial requires actual initial state evidence")
    if evidence.get("rag_enabled") is not False:
        raise ValueError("controlled initial state must match the RAG-disabled profile")
    counts = evidence.get("row_counts")
    expected_tables = set(PERSONA_MEMORY_TABLES) | {"conversation_turns", "screen_turn_provenance"}
    if not isinstance(counts, dict) or set(counts) != expected_tables or any(type(value) is not int or value != 0 for value in counts.values()):
        raise ValueError("controlled state requires observed empty tables")
    versions = evidence.get("schema_versions")
    if not isinstance(versions, dict) or set(versions) != {"history", "memory"} or any(type(value) is not int or value < 0 for value in versions.values()):
        raise ValueError("controlled state requires schema versions")
    configs = evidence.get("configuration_sha256")
    expected_files = {"characters/miori/miori.card.json", "backend/app/memory/memory_policy.json"}
    if not isinstance(configs, dict) or set(configs) != expected_files or any(
        not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
        for value in configs.values()
    ):
        raise ValueError("controlled state requires configuration hashes")
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if trial.get("initial_state_hash") != digest:
        raise ValueError("controlled state hash does not match the evidence")


def finalize_livekit_pilot(
    *, manifest_path: Path, trace_path: Path, output_path: Path,
    schema_path: Path, profile_report_path: Path, run_id: str,
    resource_observations_path: Path | None = None,
) -> None:
    """少数試行用。正式100試行の代用としては使わない。"""
    _finalize_livekit_report(manifest_path=manifest_path, trace_path=trace_path,
                            output_path=output_path, schema_path=schema_path,
                            profile_report_path=profile_report_path, run_id=run_id, controlled=False,
                            resource_observations_path=resource_observations_path)


def finalize_livekit_controlled(
    *, manifest_path: Path, trace_path: Path, output_path: Path,
    schema_path: Path, profile_report_path: Path, run_id: str,
    resource_observations_path: Path | None = None,
) -> None:
    """5 warm-up + 独立100試行のartifactを出力する。品質の合格判定は含まない。"""
    _finalize_livekit_report(manifest_path=manifest_path, trace_path=trace_path,
                            output_path=output_path, schema_path=schema_path,
                            profile_report_path=profile_report_path, run_id=run_id, controlled=True,
                            resource_observations_path=resource_observations_path)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="LiveKitの匿名集計。試行証拠の検証と品質合否は分離する。")
    parser.add_argument("--scope", choices=("pilot", "controlled"), default="pilot")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--profile-report", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resource-observations", type=Path)
    arguments = vars(parser.parse_args(argv))
    finalize = finalize_livekit_controlled if arguments["scope"] == "controlled" else finalize_livekit_pilot
    finalize(
        manifest_path=arguments["manifest"], trace_path=arguments["trace"],
        output_path=arguments["output"], schema_path=arguments["schema"],
        profile_report_path=arguments["profile_report"], run_id=arguments["run_id"],
        resource_observations_path=arguments["resource_observations"],
    )


if __name__ == "__main__":
    main()
