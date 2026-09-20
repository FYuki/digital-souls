"""固定upstream・直列worker専用の登録参照音声latent再利用。"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import math
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

REFERENCE_SOURCE_SHA256 = "8b6be6a04c94b0f6b697232b7c120f9a3d474d7db95ae24516f9261c320726a9"
MAX_REFERENCE_BYTES = 16 * 1024 * 1024
MAX_LATENT_BYTES = 2 * 1024 * 1024


def _file_digest(path: str) -> str | None:
    # mtimeだけに依存せず、同じpath・sizeの内容変更も検知する。
    try:
        with Path(path).open("rb") as source:
            content = source.read(MAX_REFERENCE_BYTES + 1)
    except OSError:
        return None
    if len(content) > MAX_REFERENCE_BYTES:
        return None
    return hashlib.sha256(content).hexdigest()


class ReferenceLatentCache:
    """runtimeごとに1件。返却tensorへの変更を保存済みlatentへ伝播させない。"""

    def __init__(self, torch: Any) -> None:
        self.torch = torch
        self.entry: tuple[Any, tuple[Any, Any], tuple[str, ...]] | None = None

    def clear(self) -> None:
        self.entry = None

    def _key(self, runtime: Any, req: Any, batch_size: int) -> Any:
        if (
            not self.torch.is_inference_mode_enabled()
            or runtime.model.training or runtime.codec.model.training
            or not runtime.key.codec_deterministic_encode
            or runtime.key.compile_model
            or not runtime.model_cfg.use_speaker_condition_resolved
            or req.no_ref or req.ref_wavs or req.ref_latent or req.ref_latents
            or req.ref_embed or req.lora_adapter
            or type(req.ref_wav) is not str or not req.ref_wav.strip()
            or type(batch_size) is not int or batch_size != 1
            or type(req.ref_ensure_max) is not bool
        ):
            return None
        maximum = runtime.default_max_ref_seconds if req.max_ref_seconds is None else req.max_ref_seconds
        for value in (maximum, req.ref_normalize_db):
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value)
            ):
                return None
        digest = _file_digest(req.ref_wav)
        if digest is None:
            return None
        return (
            runtime.model, runtime.codec, runtime.key, runtime.model_device,
            runtime.codec_device, next(runtime.model.parameters()).dtype,
            runtime.model_cfg.latent_dim, runtime.model_cfg.latent_patch_size,
            runtime.model_cfg.speaker_patch_size, runtime.codec.sample_rate,
            runtime.codec.model.hop_length, req.ref_wav, digest, batch_size,
            maximum, req.ref_normalize_db, req.ref_ensure_max,
        )

    def load(
        self, loader: Callable[..., Any], runtime: Any, *,
        req: Any, batch_size: int, messages: list[str],
    ) -> Any:
        key = self._key(runtime, req, batch_size)
        if key is not None and self.entry is not None and self.entry[0] == key:
            result = tuple(value.clone() for value in self.entry[1])
            messages.extend(self.entry[2])
            return result
        # 不一致・非対応条件・失敗を跨いで古い声のentryを残さない。
        self.clear()
        before = len(messages)
        result = loader(runtime, req=req, batch_size=batch_size, messages=messages)
        if key is None or not isinstance(result, tuple) or len(result) != 2:
            return result
        if not all(
            isinstance(value, self.torch.Tensor) and value.is_cuda and not value.requires_grad
            for value in result
        ):
            return result
        size = sum(value.numel() * value.element_size() for value in result)
        # 変換中に参照ファイル等が変化した結果は保存しない。
        if size <= MAX_LATENT_BYTES and self._key(runtime, req, batch_size) == key:
            self.entry = (
                key, (result[0].clone(), result[1].clone()), tuple(messages[before:]),
            )
        return result


def wrap_reference_loader(torch: Any, loader: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(loader)
    def load(runtime: Any, *, req: Any, batch_size: int, messages: list[str]) -> Any:
        cache = getattr(runtime, "_ds_reference_latent_cache", None)
        if cache is None:
            cache = ReferenceLatentCache(torch)
            runtime._ds_reference_latent_cache = cache
        # upstreamの_infer_lock内からだけ呼ばれ、worker終了時にruntimeごと解放される。
        return cache.load(loader, runtime, req=req, batch_size=batch_size, messages=messages)

    return load


def install_reference_latent_cache() -> None:
    # 通常起動・CPU単体テストではtorchをimportしない。
    torch = importlib.import_module("torch")
    runtime = importlib.import_module("irodori_tts.inference_runtime")
    loader = runtime.InferenceRuntime._load_reference_latent
    source = inspect.getsource(loader).encode()
    if hashlib.sha256(source).hexdigest() != REFERENCE_SOURCE_SHA256:
        raise RuntimeError("irodori_reference_cache_upstream_mismatch")
    if not torch.cuda.is_available():
        raise RuntimeError("irodori_reference_cache_cuda_required")
    runtime.InferenceRuntime._load_reference_latent = wrap_reference_loader(torch, loader)
