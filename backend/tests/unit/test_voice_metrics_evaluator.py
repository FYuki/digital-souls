from __future__ import annotations

import pytest


def _evaluator():
    return pytest.importorskip(
        "app.voice_metrics",
        reason="音声計測moduleの実装後に有効化する契約テストです",
    )


def _artifact(metrics, *, transport: str, cpu_percent: float):
    return metrics.AggregateArtifact.model_validate(
        {
            "schema_version": "1.0",
            "measurement_kind": "controlled_baseline",
            "transport": transport,
            "run_id": f"{transport}-run",
            "profile": "integration-voice",
            "fixture_version": "speech-v2",
            "quantile_method": "hyndman_fan_type_7",
            "clocks": [],
            "run_counts": {
                "warmup": 5,
                "measured": 100,
                "success": 100,
                "failure": 0,
                "excluded": 0,
            },
            "metrics": [],
            "stage_outcomes": [],
            "whisper": {
                "model": "medium",
                "device": "cpu",
                "compute_type": "int8",
            },
            "hardware": {"description": "test hardware"},
            "resources": {
                "cpu_percent": {"status": "measured", "value": cpu_percent},
                "memory_bytes": {"status": "measured", "value": 1024},
            },
            "network": {
                "sent_bytes": {"status": "measured", "value": 100},
                "received_bytes": {"status": "measured", "value": 200},
                "packet_loss_basis_points": {"status": "measured", "value": 0},
                "condition": "local",
            },
        }
    )


@pytest.mark.parametrize(
    ("metric_name", "limit_ms"),
    [
        ("ttfa", 2_000.0),
        ("local_playback_stop", 150.0),
        ("turn_decision", 300.0),
        ("cancel_after_decision", 200.0),
        ("barge_in_cancel_total", 500.0),
        ("utterance_finalized", 800.0),
    ],
)
def test_vm_target_01_absolute_latency_uses_p95_boundary(
    metric_name: str,
    limit_ms: float,
) -> None:
    metrics = _evaluator()

    passing = metrics.evaluate_latency_target(
        metric_name=metric_name,
        p50_ms=limit_ms + 1_000,
        p95_ms=limit_ms,
        websocket_p95_ms=None,
    )
    failing = metrics.evaluate_latency_target(
        metric_name=metric_name,
        p50_ms=1.0,
        p95_ms=limit_ms + 0.001,
        websocket_p95_ms=None,
    )

    assert passing.passed is True
    assert failing.passed is False


@pytest.mark.parametrize(
    ("websocket_p95_ms", "passing_p95_ms", "failing_p95_ms"),
    [(1_000.0, 1_100.0, 1_100.001), (200.0, 250.0, 250.001)],
)
def test_vm_target_01_relative_limit_uses_larger_of_ten_percent_or_50ms(
    websocket_p95_ms: float,
    passing_p95_ms: float,
    failing_p95_ms: float,
) -> None:
    metrics = _evaluator()

    passing = metrics.evaluate_relative_latency(passing_p95_ms, websocket_p95_ms)
    failing = metrics.evaluate_relative_latency(failing_p95_ms, websocket_p95_ms)

    assert passing.passed is True
    assert failing.passed is False


def test_vm_target_01_requires_both_absolute_and_relative_targets() -> None:
    metrics = _evaluator()

    absolute_only = metrics.evaluate_latency_target(
        metric_name="ttfa",
        p50_ms=900.0,
        p95_ms=1_900.0,
        websocket_p95_ms=1_000.0,
    )
    relative_only = metrics.evaluate_latency_target(
        metric_name="ttfa",
        p50_ms=900.0,
        p95_ms=2_050.0,
        websocket_p95_ms=2_000.0,
    )

    assert absolute_only.absolute_passed is True
    assert absolute_only.relative_passed is False
    assert absolute_only.passed is False
    assert relative_only.absolute_passed is False
    assert relative_only.relative_passed is True
    assert relative_only.passed is False


def test_vm_target_01_ttfa_p50_is_a_guide_not_an_acceptance_input() -> None:
    metrics = _evaluator()

    low_median = metrics.evaluate_latency_target(
        metric_name="ttfa",
        p50_ms=1_000.0,
        p95_ms=2_000.0,
        websocket_p95_ms=None,
    )
    high_median = metrics.evaluate_latency_target(
        metric_name="ttfa",
        p50_ms=1_001.0,
        p95_ms=2_000.0,
        websocket_p95_ms=None,
    )

    assert low_median.median_guide_met is True
    assert high_median.median_guide_met is False
    assert low_median.passed == high_median.passed


def test_vm_resource_01_diagnostics_do_not_change_acceptance() -> None:
    metrics = _evaluator()
    candidate = _artifact(metrics, transport="livekit", cpu_percent=10.0)
    baseline = _artifact(metrics, transport="websocket", cpu_percent=10.0)

    ordinary = metrics.evaluate_artifact(candidate, baseline)
    extreme = metrics.evaluate_artifact(
        _artifact(metrics, transport="livekit", cpu_percent=100.0), baseline
    )

    assert extreme.passed == ordinary.passed
    assert extreme.metric_results == ordinary.metric_results


def test_vm_target_01_reconnect_contract_uses_defined_denominators() -> None:
    metrics = _evaluator()

    passing = metrics.evaluate_reconnect(
        trials=100,
        recovered_within_ten_seconds=99,
        successful_recovery_ms=[1_000.0] * 99,
        duplicate_playbacks=0,
    )
    wrong_denominator = metrics.evaluate_reconnect(
        trials=99,
        recovered_within_ten_seconds=99,
        successful_recovery_ms=[1_000.0] * 99,
        duplicate_playbacks=0,
    )

    assert passing.passed is True
    assert wrong_denominator.passed is False


def test_vm_target_01_vad_contract_keeps_boundary_and_pause_results_separate() -> None:
    metrics = _evaluator()

    result = metrics.evaluate_vad(
        trials=100,
        leading_losses_over_100_ms=1,
        early_ends_over_100_ms=1,
        utterance_finalize_p95_ms=800.0,
        intentional_pause_ms=600,
        splits_at_intentional_pause=1,
    )

    assert result.passed is True
    assert result.leading_loss_rate_basis_points == 100
    assert result.early_end_rate_basis_points == 100
    assert result.split_rate_basis_points == 100


def test_vm_target_01_turn_classification_excludes_indeterminate() -> None:
    metrics = _evaluator()

    result = metrics.evaluate_turn_classification(
        backchannel_trials=100,
        interruption_trials=100,
        false_cancels=2,
        missed_interruptions=1,
        indeterminate=7,
    )

    assert result.passed is True
    assert result.false_cancel_rate_basis_points == 200
    assert result.missed_interruption_rate_basis_points == 100
    assert result.indeterminate_count == 7
    assert result.false_cancel_denominator == 100
    assert result.missed_interruption_denominator == 100


def test_vm_target_01_continuity_processing_and_presentation_contracts() -> None:
    metrics = _evaluator()

    result = metrics.evaluate_quality_targets(
        controlled_underruns=0,
        controlled_processing_failures=0,
        dogfood_gap_ms=1,
        dogfood_playback_ms=1_000,
        maximum_continuous_gap_ms=200,
        dogfood_processing_failures=1,
        dogfood_response_utterances=100,
        unexpected_session_ends=0,
        stale_presented=0,
        duplicate_playbacks=0,
        required_manual_operations=0,
    )

    assert result.continuity_passed is True
    assert result.processing_passed is True
    assert result.presentation_passed is True
