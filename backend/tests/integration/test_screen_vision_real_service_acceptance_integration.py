from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
import json
import os
from statistics import median, quantiles
import subprocess
from time import perf_counter

from PIL import Image, ImageDraw
import pytest

from app.inference import InferenceImagePart, InferenceTarget
from app.inference.runtime import create_inference_runtime
from app.screen_perception.vision import VisionInferenceClient


pytestmark = pytest.mark.inference_real


@dataclass(frozen=True)
class VisionCase:
    case_id: str
    question: str
    expected_status: str
    image: InferenceImagePart


def _commit_sha() -> str:
    configured = os.environ.get("INFERENCE_ACCEPTANCE_COMMIT_SHA")
    if configured:
        return configured
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _image(
    *,
    boxes: tuple[tuple[tuple[int, int, int, int], str, str], ...] = (),
) -> InferenceImagePart:
    canvas = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((24, 20), "PUBLIC SYNTHETIC SCREEN", fill="black")
    for coordinates, color, label in boxes:
        draw.rectangle(coordinates, fill=color, outline="black", width=3)
        draw.text((coordinates[0] + 8, coordinates[1] + 8), label, fill="white")
    encoded = BytesIO()
    canvas.save(encoded, format="PNG")
    return InferenceImagePart(encoded.getvalue(), "image/png", 640, 360)


def _cases() -> tuple[VisionCase, ...]:
    return (
        VisionCase(
            "single-warning",
            "中央の赤い警告は何ですか。画像で確認できた対象だけ答えてください。",
            "identified",
            _image(boxes=(((220, 120, 420, 250), "red", "WARNING 42"),)),
        ),
        VisionCase(
            "single-blue-action",
            "右下の青い操作は何ですか。画像で確認できた対象だけ答えてください。",
            "identified",
            _image(boxes=(((430, 245, 610, 330), "blue", "CONTINUE"),)),
        ),
        VisionCase(
            "multiple-red-candidates",
            "赤い表示は何ですか。候補が複数なら1つに決めないでください。",
            "multiple_candidates",
            _image(
                boxes=(
                    ((40, 80, 230, 170), "red", "NOTICE 3"),
                    ((360, 190, 600, 300), "red", "WARNING 7"),
                )
            ),
        ),
        VisionCase(
            "target-disappeared",
            "赤い警告は今もありますか。見つからなければ推測しないでください。",
            "not_found",
            _image(boxes=(((220, 120, 420, 250), "blue", "ALL CLEAR"),)),
        ),
        VisionCase(
            "small-text-unreadable",
            "中央の灰色領域にある極小文字を正確に読んでください。判読できなければその旨を答えてください。",
            "unreadable",
            _image(boxes=(((150, 100, 490, 260), "gray", ". . ."),)),
        ),
    )


def _p95(values: list[float]) -> float:
    if len(values) == 1:
        return values[0]
    return quantiles(values, n=100, method="inclusive")[94]


def test_screen_vision_real_model_quality() -> None:
    if os.environ.get("RUN_SCREEN_VISION_REAL_TESTS") != "true":
        pytest.skip("RUN_SCREEN_VISION_REAL_TESTS=true の明示時だけ実行する")
    provider = os.environ.get("SCREEN_VISION_ACCEPTANCE_PROVIDER")
    if provider not in {"ollama", "openai-api"}:
        pytest.fail(
            "SCREEN_VISION_ACCEPTANCE_PROVIDER must be ollama or openai-api"
        )

    runtime = create_inference_runtime(os.environ)
    evidence_cases: list[dict[str, object]] = []
    latencies: list[float] = []
    target = runtime.settings.targets.get(InferenceTarget.VISION)
    try:
        runtime.probe_startup()
        if target is None or target.reference.provider_id != provider:
            pytest.fail("Vision Target must use the selected acceptance provider")
        client = VisionInferenceClient(router=runtime.router)
        for case in _cases():
            started = perf_counter()
            try:
                observation = client.observe(
                    question=case.question,
                    image=case.image,
                    target_hint="公開合成画面内の質問対象だけを確認する。",
                )
            except Exception:
                elapsed_ms = (perf_counter() - started) * 1_000
                latencies.append(elapsed_ms)
                evidence_cases.append(
                    {
                        "case_id": case.case_id,
                        "expected_status": case.expected_status,
                        "actual_status": "inference_failure",
                        "passed": False,
                        "latency_ms": elapsed_ms,
                    }
                )
                raise
            elapsed_ms = (perf_counter() - started) * 1_000
            latencies.append(elapsed_ms)
            evidence_cases.append(
                {
                    "case_id": case.case_id,
                    "expected_status": case.expected_status,
                    "actual_status": observation.target_status,
                    "passed": observation.target_status == case.expected_status,
                    "latency_ms": elapsed_ms,
                }
            )

        identified = [
            case for case in evidence_cases if case["expected_status"] == "identified"
        ]
        target_identification_rate = sum(
            case["passed"] is True for case in identified
        ) / len(identified)
        status_conformance_rate = sum(
            case["passed"] is True for case in evidence_cases
        ) / len(evidence_cases)
        assert target_identification_rate >= 0.85
        assert status_conformance_rate == 1.0
    finally:
        print(
            json.dumps(
                {
                    "screen_vision_acceptance": {
                        "executed_at": datetime.now(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "commit_sha": _commit_sha(),
                        "environment": os.environ.get(
                            "INFERENCE_ACCEPTANCE_ENVIRONMENT", "dev"
                        ),
                        "provider": provider,
                        "model": (
                            target.reference.model_id if target is not None else None
                        ),
                        "case_count": len(evidence_cases),
                        "target_identification_rate": (
                            sum(
                                case["passed"] is True
                                for case in evidence_cases
                                if case["expected_status"] == "identified"
                            )
                            / max(
                                1,
                                sum(
                                    case["expected_status"] == "identified"
                                    for case in evidence_cases
                                ),
                            )
                        ),
                        "status_conformance_rate": (
                            sum(case["passed"] is True for case in evidence_cases)
                            / max(1, len(evidence_cases))
                        ),
                        "vision_latency_p50_ms": (
                            median(latencies) if latencies else None
                        ),
                        "vision_latency_p95_ms": (
                            _p95(latencies) if latencies else None
                        ),
                        "cases": evidence_cases,
                    }
                },
                ensure_ascii=False,
            )
        )
        runtime.close()
