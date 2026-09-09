"""固定PCMで、待機後のSTT時間と無音による事前準備の効果を比較する。"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
import unicodedata
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def run(output: Path) -> bool:
    sys.path.insert(0, str(ROOT / "backend"))
    from app.conversation_core.adapters import _resample_pcm16
    from app.livekit_transport.stt_audio import prepare_stt_audio

    fixture = ROOT / "frontend/playwright/fixtures"
    metadata = json.loads((fixture / "speech.metadata.json").read_text())
    source = fixture / "speech.wav"
    if hashlib.sha256(source.read_bytes()).hexdigest() != metadata["audio_sha256"]:
        raise ValueError("fixture hash mismatch")
    with wave.open(str(source)) as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("fixture must be mono PCM16")
        raw = wav.readframes(wav.getnframes())
        rate = wav.getframerate()
    samples = _resample_pcm16(list(struct.unpack(f"<{len(raw) // 2}h", raw)),
                             input_sample_rate=rate, output_sample_rate=16000)
    body = prepare_stt_audio(struct.pack(f"<{len(samples)}h", *samples))[0]
    rows: list[dict[str, object]] = []
    report = {"scope": "stt_idle_diagnostic", "fixture_sha256": metadata["audio_sha256"], "trials": rows}
    with output.open("x") as destination:
        try:
            with httpx.Client(timeout=55, trust_env=False) as client:
                for pause, prepare in [(0, False), (.2, False), (.5, False), (1, False),
                                       (2, False), (4, False), (4, True), (4, True)]:
                    time.sleep(pause)
                    preparation_ms = None
                    if prepare:
                        started = time.perf_counter_ns()
                        response = client.post("http://127.0.0.1:50022/v1/transcriptions", content=bytes(3200),
                                               headers={"Content-Type": "application/octet-stream"})
                        response.raise_for_status()
                        preparation_ms = (time.perf_counter_ns() - started) / 1e6
                        time.sleep(.4)
                    started = time.perf_counter_ns()
                    response = client.post("http://127.0.0.1:50022/v1/transcriptions", content=body,
                                           headers={"Content-Type": "application/octet-stream"})
                    response.raise_for_status()
                    transcript = response.json()["text"]
                    normalized = "".join(c for c in unicodedata.normalize("NFKC", transcript) if c.isalnum())
                    row = {"idle_seconds": pause, "silence_prewarm": prepare,
                           "after_prewarm_delay_seconds": .4 if prepare else None,
                           "warmup_ms": preparation_ms,
                           "recognition_ms": (time.perf_counter_ns() - started) / 1e6,
                           "matches": normalized == metadata["expected_transcript"]}
                    rows.append(row)
                    print(json.dumps(row), flush=True)
        finally:
            # 失敗以前の数値も保存し、本文・音声は記録しない。
            json.dump(report, destination, indent=2, allow_nan=False)
            destination.write("\n")
    return len(rows) == 8 and all(row["matches"] is True for row in rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if run(args.output) else 1)
