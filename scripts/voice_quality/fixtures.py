"""#150の固定ラベル付き音声fixture。利用者の会話・運用データは読み取らない。"""
from __future__ import annotations

import argparse
import array
import hashlib
import io
import json
import math
import sys
import wave
from pathlib import Path

SAMPLE_RATE = 48_000
BOUNDARY_WINDOW = 480
BOUNDARY_RMS = 200
ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = Path(__file__).with_name("fixture_cases.json")
DEFAULT_ROOT = ROOT / "frontend/playwright/fixtures/voice-quality-v1"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_pcm(wav_bytes: bytes) -> array.array[int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
            raise ValueError("fixture must be mono 48 kHz PCM16")
        values = array.array("h", audio.readframes(audio.getnframes()))
    if sys.byteorder != "little":
        values.byteswap()
    return values


def encode_pcm(values: array.array[int]) -> bytes:
    data = array.array("h", values)
    if sys.byteorder != "little":
        data.byteswap()
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(data.tobytes())
    return out.getvalue()


def speech_bounds(values: array.array[int]) -> tuple[int, int]:
    # VAD出力から正解を作らない。固定の信号境界法を全sourceに同じ条件で適用する。
    active = []
    for start in range(0, len(values), BOUNDARY_WINDOW):
        frame = values[start:start + BOUNDARY_WINDOW]
        if frame and math.sqrt(sum(v * v for v in frame) / len(frame)) > BOUNDARY_RMS:
            active.append(start)
    if not active:
        raise ValueError("fixture source has no annotated speech")
    return active[0], min(len(values), active[-1] + BOUNDARY_WINDOW)


def materialize_trial(root: Path, trial: dict[str, object]) -> tuple[bytes, dict[str, object]]:
    clips = []
    gain = float(trial["gain"])
    for segment in trial["segments"]:
        assert isinstance(segment, dict)
        path = root / str(segment["source_file"])
        if path.parent.resolve() != root.resolve() or path.suffix != ".wav":
            raise ValueError("fixture source must be a local WAV basename")
        data = path.read_bytes()
        if sha256(data) != segment["source_sha256"]:
            raise ValueError("fixture source hash mismatch")
        pcm = read_pcm(data)
        start, end = int(segment["speech_start_sample"]), int(segment["speech_end_sample"])
        if not 0 <= start < end <= len(pcm):
            raise ValueError("invalid fixture source boundary")
        clips.append(array.array("h", (max(-32768, min(32767, round(v * gain))) for v in pcm[start:end])))
    values = array.array("h", [0]) * int(trial["leading_samples"])
    intervals = []
    for index, clip in enumerate(clips):
        if index:
            values.extend(array.array("h", [0]) * int(trial["pause_samples"]))
        start = len(values)
        values.extend(clip)
        intervals.append({"start_sample": start, "end_sample": len(values)})
    values.extend(array.array("h", [0]) * int(trial["trailing_samples"]))
    result = encode_pcm(values)
    return result, {"audio_sha256": sha256(result), "sample_rate_hz": SAMPLE_RATE,
                    "speech_intervals": intervals, "expected_utterances": 1}


def build_manifest(root: Path, cases_document: dict[str, object], sources: list[dict[str, object]]) -> dict[str, object]:
    trials: list[dict[str, object]] = []
    for cohort in ("backchannel", "take_turn", "pause"):
        cohort_sources = [s for s in sources if s["cohort"] == ("take_turn" if cohort == "pause" else cohort)]
        for source in cohort_sources:
            for variation in range(10):
                # 10語句×10位相・音量条件。100種類の独立した人間の発声とは扱わない。
                trial = {"id": f"{cohort}-{source['id']}-{variation:02d}", "cohort": cohort,
                         "gain": (0.7, 0.85, 1.0, 1.15, 1.25)[variation % 5],
                         "leading_samples": 4800 + variation * 432,
                         "trailing_samples": SAMPLE_RATE * 2,
                         "pause_samples": (200, 400, 600)[variation % 3] * 48 if cohort == "pause" else 0,
                         "segments": [source] * (2 if cohort == "pause" else 1)}
                _, annotation = materialize_trial(root, trial)
                trial.update(annotation)
                trials.append(trial)
    return {"version": cases_document["version"], "cases_sha256": sha256(CASES_PATH.read_bytes()),
            "label_method": "predeclared_conversational_intent",
            "boundary_method": {"kind": "source_pcm_rms_window", "window_samples": BOUNDARY_WINDOW,
                                "threshold_pcm16": BOUNDARY_RMS, "resolution_ms": 10},
            "sources": sources, "trials": trials}


def generate(root: Path, url: str, speaker: int) -> None:
    import httpx
    cases = json.loads(CASES_PATH.read_text())
    root.mkdir(parents=True, exist_ok=True)
    # 既存versionを黙って別音声へ変更しない。
    if (root / "manifest.json").exists():
        raise ValueError("fixture version already exists; validate or choose a new directory")
    sources = []
    with httpx.Client(base_url=url, timeout=60) as client:
        version_response = client.get("/version")
        version_response.raise_for_status()
        engine_version = version_response.json()
        provenance = {"cases_sha256": sha256(CASES_PATH.read_bytes()), "speaker_id": speaker,
                      "engine_version": engine_version, "sample_rate_hz": SAMPLE_RATE}
        provenance_path = root / "generation.json"
        if provenance_path.exists():
            if json.loads(provenance_path.read_text()) != provenance:
                raise ValueError("fixture generation settings changed after partial generation")
        elif any(root.glob("*.wav")):
            raise ValueError("fixture source provenance is unavailable")
        else:
            provenance_path.write_text(json.dumps(provenance, indent=2) + "\n")
        for case in cases["cases"]:
            name = f"{case['id']}.wav"
            path = root / name
            # 中断後の同version再開では、既存の生成済みsourceを保持する。
            if not path.exists():
                response = client.post("/audio_query", params={"text": case["text"], "speaker": speaker})
                response.raise_for_status()
                query = response.json()
                query.update(outputSamplingRate=SAMPLE_RATE, outputStereo=False, prePhonemeLength=0.1, postPhonemeLength=0.1)
                result = client.post("/synthesis", params={"speaker": speaker}, json=query)
                result.raise_for_status()
                read_pcm(result.content)
                path.write_bytes(result.content)
            data = path.read_bytes()
            pcm = read_pcm(data)
            start, end = speech_bounds(pcm)
            sources.append({"id": case["id"], "cohort": case["cohort"], "source_file": name,
                            "source_sha256": sha256(data), "speech_start_sample": start,
                            "speech_end_sample": end, "speaker_id": speaker, "engine_version": engine_version})
    manifest = build_manifest(root, cases, sources)
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"sources": len(sources), "trials": len(manifest["trials"]), "manifest": str(root / "manifest.json")}))


def validate(root: Path, output: Path | None) -> None:
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest["cases_sha256"] != sha256(CASES_PATH.read_bytes()):
        raise ValueError("fixture labels changed after generation")
    cases = json.loads(CASES_PATH.read_text())
    sources = manifest["sources"]
    if len(sources) != len(cases["cases"]):
        raise ValueError("fixture source set changed")
    for source, case in zip(sources, cases["cases"], strict=True):
        if source["id"] != case["id"] or source["cohort"] != case["cohort"]:
            raise ValueError("fixture labels differ from declared intent")
        if source["source_file"] != f"{case['id']}.wav":
            raise ValueError("fixture source filename changed")
        data = (root / source["source_file"]).read_bytes()
        if sha256(data) != source["source_sha256"]:
            raise ValueError("fixture source hash mismatch")
        if speech_bounds(read_pcm(data)) != (source["speech_start_sample"], source["speech_end_sample"]):
            raise ValueError("fixture boundary changed")
    if manifest != build_manifest(root, cases, sources):
        raise ValueError("fixture recipes differ from fixed cohort definitions")
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    seen = set()
    for trial in manifest["trials"]:
        if trial["id"] in seen:
            raise ValueError("duplicate fixture trial")
        seen.add(trial["id"])
        wav, annotation = materialize_trial(root, trial)
        if any(trial[key] != value for key, value in annotation.items()):
            raise ValueError("fixture trial annotation mismatch")
        counts[trial["cohort"]] = counts.get(trial["cohort"], 0) + 1
        if output is not None:
            (output / f"{trial['id']}.wav").write_bytes(wav)
    if counts != {"backchannel": 100, "take_turn": 100, "pause": 100}:
        raise ValueError("fixture cohort must contain 100 declared trials each")
    print(json.dumps({"validated": True, "cohorts": counts}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "validate", "materialize"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--voicevox-url", default="http://127.0.0.1:50021")
    parser.add_argument("--speaker", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "generate":
        generate(args.root, args.voicevox_url, args.speaker)
    else:
        if args.command == "materialize" and args.output is None:
            parser.error("materialize requires --output")
        validate(args.root, args.output if args.command == "materialize" else None)


if __name__ == "__main__":
    main()
