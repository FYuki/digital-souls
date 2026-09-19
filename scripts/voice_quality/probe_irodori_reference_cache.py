"""専用のCUDA Graph候補イメージで固定文の合成時間を対比較する。

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

MEASUREMENTS = []
CAPTURE_ERRORS = []

STAGES = frozenset({
    "tokenize_text", "prepare_reference", "predict_duration", "sample_rf",
    "unpatchify_latent", "decode_latent", "silentcipher_watermark",
})
TEXTS = ("こんにちは。", "今日はいい天気ですね。", "少し休憩しましょう。", "窓の外を眺めていたら、小さな鳥が木の枝にとまっていました。", "明日は朝から図書館に出かけて、気になっていた本を探す予定です。そのあと、公園をゆっくり歩いてから帰りたいと思います。")


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

    import irodori_tts.inference_runtime as runtime_module
    import numpy as np
    import irodori_tts.model as model_module
    baseline_sampler = runtime_module.sample_euler_rf_cfg
    from irodori_service.cuda_graph import install_cuda_graph_sampler, RequestGraph
    install_cuda_graph_sampler()
    graph_sampler = runtime_module.sample_euler_rf_cfg
    baseline_sampler = graph_sampler
    runtime_module.sample_euler_rf_cfg = graph_sampler
    graph_statistics = []
    graph_init = RequestGraph.__init__
    graph_call = RequestGraph.__call__
    def counted_init(graph, *args, **kwargs):
        graph_init(graph, *args, **kwargs)
        graph.diagnostic = {"calls": 0, "captures": 0}
        graph_statistics.append(graph.diagnostic)
    def counted_call(graph, **kwargs):
        before = len(graph.entries)
        output = graph_call(graph, **kwargs)
        graph.diagnostic["calls"] += 1
        graph.diagnostic["captures"] += max(0, len(graph.entries) - before)
        return output
    RequestGraph.__init__ = counted_init
    RequestGraph.__call__ = counted_call
    # 同形状の条件更新と出力の独立性を実GPUで検証する。
    with torch.inference_mode():
        graph = RequestGraph(torch, lambda x, nested: x + nested["kv"][0])
        x = torch.ones(4, device="cuda")
        condition = torch.ones(4, device="cuda")
        first = graph(x=x, nested={"kv": [condition]})
        condition.mul_(3)
        second = graph(x=x, nested={"kv": [condition]})
        torch.cuda.synchronize()
        assert torch.equal(first, torch.full_like(first, 2))
        assert torch.equal(second, torch.full_like(second, 4))
        assert len(graph.entries) == 1
        for size in (8, 12):
            result = graph(x=torch.ones(size, device="cuda"), nested={"kv": [torch.ones(size, device="cuda")]})
            assert torch.equal(result, torch.full_like(result, 2))
        assert len(graph.entries) == 2
        graph.entries.clear()
    del graph, first, second, x, condition, result
    graph_statistics.clear()
    reference_pcm = {}

    timings: list[dict] = []
    condition_statistics = []
    reuse_enabled = False
    verify_reuse = False
    original = InferenceRuntime.synthesize

    from irodori_service.reference_cache import install_reference_latent_cache
    reference_loader = InferenceRuntime._load_reference_latent
    install_reference_latent_cache()
    product_loader = InferenceRuntime._load_reference_latent
    def selected_reference(runtime, *, req, batch_size, messages):
        statistic = {"enabled": reuse_enabled, "hits": 0}
        condition_statistics.append(statistic)
        if not reuse_enabled:
            return reference_loader(runtime, req=req, batch_size=batch_size, messages=messages)
        cache = getattr(runtime, "_ds_reference_latent_cache", None)
        before_entry = None if cache is None else cache.entry
        before_messages = len(messages)
        result = product_loader(runtime, req=req, batch_size=batch_size, messages=messages)
        after_entry = runtime._ds_reference_latent_cache.entry
        statistic["hits"] = int(before_entry is not None and before_entry is after_entry)
        statistic["cached"] = after_entry is not None
        if verify_reuse:
            actual_messages = []
            actual = reference_loader(runtime, req=req, batch_size=batch_size, messages=actual_messages)
            assert all(torch.equal(x,y) for x,y in zip(actual, result)), "reference_output_mismatch"
            assert actual_messages == messages[before_messages:], "reference_messages_mismatch"
            statistic["exact_repeated_output_verified"] = True
        return result
    InferenceRuntime._load_reference_latent = selected_reference

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
            pcm = np.frombuffer(wav.readframes(frames), dtype="<i2").astype(np.float64)
        reference = reference_pcm.setdefault(text, pcm.copy())
        comparison = {"same_frames": len(reference) == len(pcm)}
        if len(reference) == len(pcm):
            diff = pcm - reference
            comparison.update(pcm_equal=bool(np.array_equal(pcm, reference)),
                              maximum_absolute_pcm16=float(np.max(np.abs(diff))),
                              rms_error_pcm16=float(np.sqrt(np.mean(diff * diff))),
                              reference_rms_pcm16=float(np.sqrt(np.mean(reference * reference))))
        row = {
            "comparison": comparison, "phase": phase, "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "elapsed_ms": elapsed, "wav_sha256": hashlib.sha256(audio).hexdigest(),
            "frames": frames, "sample_rate": rate, **timings[-1],
            "allocated_bytes": torch.cuda.memory_allocated(),
            "reserved_bytes": torch.cuda.memory_reserved(),
        }
        MEASUREMENTS.append(row)
        return row

    async def run() -> list[dict]:
        nonlocal reuse_enabled, verify_reuse
        reuse_enabled = True
        rows = [await once("こんにちは。音声の準備ができました。", "warmup")]
        reuse_enabled = verify_reuse = True
        for text in TEXTS:
            rows.append(await once(text, "equivalence"))
        verify_reuse = False
        for repetition in range(3):
            for text in TEXTS:
                for mode in (("baseline", "reference_reuse") if repetition % 2 == 0 else ("reference_reuse", "baseline")):
                    reuse_enabled = mode == "reference_reuse"
                    row = await once(text, mode)
                    row["repetition"] = repetition
                    rows.append(row)
        runtime_module.sample_euler_rf_cfg = baseline_sampler
        return rows

    try:
        rows = asyncio.run(run())
        return {
            "status": "success", "graph_statistics": graph_statistics, "scope": "paired_reference_latent_reuse_product", "condition_statistics": condition_statistics,
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
        InferenceRuntime._load_reference_latent = reference_loader
        runtime_module.sample_euler_rf_cfg = baseline_sampler
        RequestGraph.__init__ = graph_init
        RequestGraph.__call__ = graph_call


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
                result = {"status": "failure", "error_type": type(error).__name__, "diagnostic_error": str(error)[:1200], "partial_trials": MEASUREMENTS, "capture_errors": CAPTURE_ERRORS}
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
