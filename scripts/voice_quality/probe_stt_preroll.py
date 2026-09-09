"""同じ合成音声の先頭無音だけを変え、Whisperへの入力準備を診断する。"""
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


def run(output: Path, *, prepare: bool) -> bool:
    sys.path.insert(0, str(ROOT / "backend"))
    from app.conversation_core.adapters import _resample_pcm16
    from app.livekit_transport.stt_audio import prepare_stt_audio

    fixture_root = ROOT / "frontend/playwright/fixtures"
    metadata = json.loads((fixture_root / "speech.metadata.json").read_text())
    wav_path = fixture_root / "speech.wav"
    if hashlib.sha256(wav_path.read_bytes()).hexdigest() != metadata["audio_sha256"]:
        raise ValueError("fixture hash mismatch")
    with wave.open(str(wav_path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("fixture must be mono PCM16")
        data = source.readframes(source.getnframes())
        rate = source.getframerate()
    values = struct.unpack(f"<{len(data) // 2}h", data)
    # 正解境界の前後100msを保ち、同じ波形への先頭無音だけを変える。
    segment = values[max(0, metadata["speech_start_sample"] - rate // 10):
                     metadata["speech_end_sample"] + rate // 10]
    resampled = _resample_pcm16(list(segment), input_sample_rate=rate, output_sample_rate=16_000)
    audio = struct.pack(f"<{len(resampled)}h", *resampled)
    rows: list[dict[str, object]] = []
    report = {"scope": "stt_silence_diagnostic", "input_preparation_enabled": prepare,
              "source_fixture_sha256": metadata["audio_sha256"], "trials": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as result_file:
        try:
            with httpx.Client(timeout=55, trust_env=False) as client:
                for lead in (0, 500, 1000, 1500, 2000):
                    for repeat in range(2):
                        body = bytes(lead * 32) + audio + bytes(800 * 32)
                        removed = 0
                        if prepare:
                            body, removed = prepare_stt_audio(body)
                        started = time.monotonic()
                        response = client.post(
                            "http://127.0.0.1:50022/v1/transcriptions", content=body,
                            headers={"Content-Type": "application/octet-stream"},
                        )
                        response.raise_for_status()
                        transcript = response.json()["text"]
                        normalized = "".join(char for char in unicodedata.normalize("NFKC", transcript) if char.isalnum())
                        row = {"trimmed_samples": removed, "leading_silence_ms": lead, "repeat": repeat,
                               "samples": len(body) // 2, "transcript_length": len(transcript),
                               "matches": normalized == metadata["expected_transcript"],
                               "latency_ms": round((time.monotonic() - started) * 1000, 3)}
                        rows.append(row)
                        print(json.dumps(row), flush=True)
        finally:
            # 通信が失敗しても、それ以前の試行を残す。本文は出力しない。
            json.dump(report, result_file, indent=2, allow_nan=False)
            result_file.write("\n")
    return len(rows) == 10 and all(row["matches"] is True for row in rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if run(args.output, prepare=args.prepare) else 1)
