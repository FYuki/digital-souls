"""専用の一時コンテナで既存Irodori APIの工程時間を観測する。

stdinへCCVのtts_configだけを渡す。共有サービス内では実行しない。
返すのは固定合成文のhash・時間・sample数で、音声本文や外部ログを保存しない。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import time
import wave

STAGES = frozenset({
    "tokenize_text", "prepare_reference", "predict_duration", "sample_rf",
    "unpatchify_latent", "decode_latent", "silentcipher_watermark",
})
TEXTS = ("こんにちは。", "今日はいい天気ですね。", "少し休憩しましょう。")


def measure(voice: dict) -> dict:
    from huggingface_hub import snapshot_download
    from irodori_service.config import (
        CODEC_REPOSITORY, CODEC_REVISION, MODEL_REPOSITORY, MODEL_REVISION,
    )
    from irodori_service.voices import RegisteredVoices

    model = Path(snapshot_download(
        repo_id=MODEL_REPOSITORY, revision=MODEL_REVISION,
        cache_dir="/models/huggingface", local_files_only=True,
        allow_patterns=["model.safetensors", "tokenizer/*"],
    ))
    codec = Path(snapshot_download(
        repo_id=CODEC_REPOSITORY, revision=CODEC_REVISION,
        cache_dir="/models/huggingface", local_files_only=True,
        allow_patterns=["weights.pth"],
    ))
    os.environ.update({
        "IRODORI_CHECKPOINT": str(model / "model.safetensors"),
        "IRODORI_CODEC_REPO": str(codec / "weights.pth"),
        "IRODORI_MODEL_DEVICE": "cuda", "IRODORI_CODEC_DEVICE": "cuda",
        "IRODORI_MODEL_PRECISION": "bf16", "IRODORI_CODEC_PRECISION": "bf16",
        "IRODORI_PRELOAD": "false", "IRODORI_ALLOW_NO_REF_VOICE": "false",
        "IRODORI_VOICES_DIR": "/voices",
        "IRODORI_DEFAULT_CHUNKING_ENABLED": "false",
        "IRODORI_MAX_CONCURRENT_SYNTHESIS": "1",
        "IRODORI_COMPILE_MODEL": "false",
    })
    upstream = importlib.import_module("irodori_openai_tts.app")
    from irodori_tts.inference_runtime import InferenceRuntime
    import torch

    timings: list[dict] = []
    original = InferenceRuntime.synthesize

    def observed(runtime, request, **kwargs):
        result = original(runtime, request, **kwargs)
        stages = {name: seconds * 1000 for name, seconds in result.stage_timings}
        if not set(stages) <= STAGES:
            raise ValueError("unexpected timing stage")
        timings.append({
            "stages_ms": stages, "total_to_decode_ms": result.total_to_decode * 1000,
            "seed": result.used_seed,
        })
        return result

    InferenceRuntime.synthesize = observed
    registry = RegisteredVoices(Path("/voices"))

    async def once(text: str, phase: str) -> dict:
        registry.resolve(voice["voice_id"])
        payload = upstream.SpeechRequest(
            model="irodori-tts", input=text, voice=voice["voice_id"],
            response_format="wav", speed=voice["speed"],
            irodori={"caption": voice["caption"], "seed": voice["seed"],
                     "num_steps": voice["num_steps"], "chunking_enabled": False},
        )
        before = len(timings)
        started = time.perf_counter()
        response = await upstream.create_speech(payload)
        elapsed = (time.perf_counter() - started) * 1000
        assert len(timings) == before + 1
        audio = bytes(response.body)
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
        return {
            "phase": phase, "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "elapsed_ms": elapsed, "wav_sha256": hashlib.sha256(audio).hexdigest(),
            "frames": frames, "sample_rate": rate, **timings[-1],
            "allocated_bytes": torch.cuda.memory_allocated(),
            "reserved_bytes": torch.cuda.memory_reserved(),
        }

    async def run() -> list[dict]:
        rows = [await once("こんにちは。音声の準備ができました。", "warmup")]
        rows += [await once(text, "diagnostic") for text in TEXTS]
        return rows

    try:
        rows = asyncio.run(run())
        return {
            "status": "success", "scope": "isolated_fixed_text_tts_stage_diagnostic",
            "formal_acceptance": False, "compile_model": upstream.settings.compile_model,
            "empty_cache_interval": upstream.settings.empty_cache_interval,
            "model_revision": MODEL_REVISION, "codec_revision": CODEC_REVISION,
            "voice_config_sha256": hashlib.sha256(
                json.dumps(voice, sort_keys=True, ensure_ascii=False).encode(),
            ).hexdigest(),
            "trials": rows,
        }
    finally:
        InferenceRuntime.synthesize = original


def main() -> int:
    voice = json.load(sys.stdin)
    saved_out, saved_err = os.dup(1), os.dup(2)
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            logging.disable(logging.CRITICAL)
            try:
                result = measure(voice)
            except Exception as error:
                result = {"status": "failure", "error_type": type(error).__name__}
            sys.stdout.flush()
            sys.stderr.flush()
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
