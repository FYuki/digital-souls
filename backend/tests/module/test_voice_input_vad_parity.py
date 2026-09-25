"""同一PCMに対する既存FEとBEの実モデル・WASM・区間検出を比較する。"""

from __future__ import annotations
import json
from pathlib import Path
import subprocess
import wave
import numpy as np
import pytest
from app.voice_input.pipeline import VoiceInputPipeline
from app.voice_input.models import FRAME_SAMPLES

pytestmark = pytest.mark.cross_language

ROOT = Path(__file__).resolve().parents[3]


def fixture_pcm(kind: str) -> bytes:
    if kind not in {"tone", "noise"}:
        relative = "speech.wav" if kind == "speech" else f"voice-quality-v2/{kind}.wav"
        with wave.open(str(ROOT / "frontend/playwright/fixtures" / relative)) as audio:
            assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
            rate = audio.getframerate()
            source = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        # 同じ正規化PCMを両runtimeへ渡す比較。transportのresampling受入は別に行う。
        samples = np.interp(
            np.arange(0, len(source), rate / 16000), np.arange(len(source)), source
        ).astype("<i2")
        samples = np.concatenate(
            (np.zeros(16000, dtype="<i2"), samples, np.zeros(32000, dtype="<i2"))
        )
    else:
        count = FRAME_SAMPLES * 100
        samples = np.zeros(count, dtype="<i2")
        rng = np.random.default_rng(358)
        for offset, duration, frequency in [
            (8000, 3200, 220),
            (32000, 6400, 440),
            (64000, 10400, 1000),
        ]:
            wave_samples = (
                800 * np.sin(2 * np.pi * frequency * np.arange(duration) / 16000)
                if kind == "tone"
                else rng.uniform(-800, 800, duration)
            )
            samples[offset : offset + duration] = wave_samples.astype("<i2")
    samples = np.pad(samples, (0, (-len(samples)) % FRAME_SAMPLES)).astype("<i2")
    return samples.tobytes()


@pytest.mark.parametrize(
    "kind",
    [
        "speech",
        "tone",
        "noise",
        *(
            p.stem
            for p in sorted(
                (ROOT / "frontend/playwright/fixtures/voice-quality-v2").glob("*.wav")
            )
        ),
    ],
)
def test_existing_fe_and_backend_models_and_boundaries_match(
    tmp_path: Path, kind: str
) -> None:
    pcm = fixture_pcm(kind)
    path = tmp_path / "fixed-parity.pcm"
    path.write_bytes(pcm)
    completed = subprocess.run(
        ["node", str(ROOT / "frontend/scripts/voice-vad-parity.mjs"), str(path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    baseline = json.loads(completed.stdout.strip().splitlines()[-1])
    pipeline = VoiceInputPipeline()
    frames, events, resets = [], [], []
    try:
        for offset in range(0, len(pcm), 3072):
            for frame in pipeline.feed(
                pcm[offset : offset + 3072], start_sample=offset // 2
            ):
                frames.append(
                    [
                        frame.probability,
                        frame.evidence.voiced_fraction,
                        frame.evidence.tonal_concentration,
                        frame.evidence.spectral_flatness,
                    ]
                )
                if frame.model_reset:
                    resets.append(frame.end_sample)
                events.extend(
                    {
                        "kind": d.kind,
                        "started_sample": d.started_sample,
                        "detected_sample": d.detected_sample,
                    }
                    for d in frame.detections
                )
    finally:
        pipeline.close()
    actual, expected = np.asarray(frames), np.asarray(baseline["frames"])
    # CPUとWASMは浮動小数演算が異なる。固定3系列の最大差は0.00024933。
    # 数値誤差の上限だけで合格にせず、全閾値の判定・区間・resetを別途完全一致させる。
    for threshold in (0.2, 0.25, 0.3, 0.4):
        np.testing.assert_array_equal(
            actual[:, 0] >= threshold, expected[:, 0] >= threshold
        )
    np.testing.assert_allclose(actual[:, 0], expected[:, 0], atol=5e-4, rtol=0)
    np.testing.assert_allclose(actual[:, 1:], expected[:, 1:], atol=1e-9, rtol=1e-9)
    assert events == baseline["events"]
    assert resets == baseline["resets"]
    if kind == "speech":
        assert any(e["kind"] == "confirmed" for e in events)
        assert any(e["kind"] == "ended" for e in events)
