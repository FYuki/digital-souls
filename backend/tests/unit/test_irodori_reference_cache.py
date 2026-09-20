"""声の取り違え・stale tensor・無制限な保持をCPU上で再現する。"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from irodori_service import reference_cache
from irodori_service.config import ServiceConfig, load_config


class Tensor:
    is_cuda = True
    requires_grad = False

    def __init__(self, value):
        self.value = value

    def numel(self):
        return 1

    def element_size(self):
        return 4

    def clone(self):
        return Tensor(self.value)


def setup(tmp_path):
    path = tmp_path / "reference.wav"
    path.write_bytes(b"first")
    torch = SimpleNamespace(Tensor=Tensor, is_inference_mode_enabled=lambda: True)
    model = SimpleNamespace(training=False, parameters=lambda: iter([SimpleNamespace(dtype="bf16")]))
    codec = SimpleNamespace(model=SimpleNamespace(training=False, hop_length=320), sample_rate=24000)
    runtime = SimpleNamespace(
        model=model, codec=codec, model_device="cuda:0", codec_device="cuda:0",
        key=SimpleNamespace(codec_deterministic_encode=True, compile_model=False),
        model_cfg=SimpleNamespace(use_speaker_condition_resolved=True, latent_dim=32,
                                  latent_patch_size=2, speaker_patch_size=2),
        default_max_ref_seconds=30.0,
    )
    req = SimpleNamespace(
        ref_wav=str(path), ref_wavs=None, ref_latent=None, ref_latents=None,
        ref_embed=None, lora_adapter=None, no_ref=False, ref_ensure_max=True,
        max_ref_seconds=None, ref_normalize_db=-16.0,
    )
    calls = []
    def loader(runtime, *, req, batch_size, messages):
        calls.append((req.ref_wav, batch_size))
        messages.append("参照音声の変換")
        return Tensor(len(calls)), Tensor(1)
    return torch, runtime, req, calls, loader


def test_config_is_opt_in_and_strict(monkeypatch, tmp_path):
    monkeypatch.delenv("DS_IRODORI_REFERENCE_CACHE", raising=False)
    assert not load_config().reference_cache
    monkeypatch.setenv("DS_IRODORI_REFERENCE_CACHE", "true")
    assert load_config().reference_cache
    for value in ("1", "TRUE", "yes", ""):
        monkeypatch.setenv("DS_IRODORI_REFERENCE_CACHE", value)
        with pytest.raises(ValueError, match="DS_IRODORI_REFERENCE_CACHE"):
            load_config()
    with pytest.raises(ValueError, match="boolean"):
        ServiceConfig(tmp_path, tmp_path, reference_cache="false")


def test_returned_tensors_are_independent_and_messages_are_replayed(tmp_path):
    torch, runtime, req, calls, loader = setup(tmp_path)
    load = reference_cache.wrap_reference_loader(torch, loader)
    first = load(runtime, req=req, batch_size=1, messages=[])
    first[0].value = 999
    messages = ["呼出元の既存message"]
    second = load(runtime, req=req, batch_size=1, messages=messages)
    assert second[0].value == 1
    second[0].value = 222
    third = load(runtime, req=req, batch_size=1, messages=[])
    assert third[0].value == 1
    assert len(calls) == 1
    assert messages == ["呼出元の既存message", "参照音声の変換"]


def test_same_size_same_mtime_file_change_is_not_reused(tmp_path):
    torch, runtime, req, calls, loader = setup(tmp_path)
    load = reference_cache.wrap_reference_loader(torch, loader)
    load(runtime, req=req, batch_size=1, messages=[])
    path = tmp_path / "reference.wav"
    before = path.stat()
    path.write_bytes(b"other")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert load(runtime, req=req, batch_size=1, messages=[])[0].value == 2
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [
    ("max_ref_seconds", 10.0), ("ref_normalize_db", -20.0), ("ref_ensure_max", False),
])
def test_reference_settings_invalidate_previous_entry(tmp_path, field, value):
    torch, runtime, req, calls, loader = setup(tmp_path)
    load = reference_cache.wrap_reference_loader(torch, loader)
    load(runtime, req=req, batch_size=1, messages=[])
    setattr(req, field, value)
    assert load(runtime, req=req, batch_size=1, messages=[])[0].value == 2
    assert len(calls) == 2


@pytest.mark.parametrize("condition", [
    "no_ref", "ref_wavs", "ref_latent", "ref_latents", "ref_embed", "lora_adapter",
    "training", "codec_training", "nondeterministic", "compile", "no_inference",
    "batch", "oversized", "missing", "invalid_number",
])
def test_unsupported_requests_use_original_and_clear_old_entry(tmp_path, condition, monkeypatch):
    torch, runtime, req, calls, loader = setup(tmp_path)
    load = reference_cache.wrap_reference_loader(torch, loader)
    load(runtime, req=req, batch_size=1, messages=[])
    batch_size = 1
    if condition in ("no_ref", "ref_wavs", "ref_latent", "ref_latents", "ref_embed", "lora_adapter"):
        setattr(req, condition, True)
    elif condition == "training":
        runtime.model.training = True
    elif condition == "codec_training":
        runtime.codec.model.training = True
    elif condition == "nondeterministic":
        runtime.key.codec_deterministic_encode = False
    elif condition == "compile":
        runtime.key.compile_model = True
    elif condition == "no_inference":
        torch.is_inference_mode_enabled = lambda: False
    elif condition == "batch":
        batch_size = 2
    elif condition == "oversized":
        monkeypatch.setattr(reference_cache, "MAX_REFERENCE_BYTES", 4)
    elif condition == "missing":
        (tmp_path / "reference.wav").unlink()
    else:
        req.max_ref_seconds = float("nan")
    assert load(runtime, req=req, batch_size=batch_size, messages=[])[0].value == 2
    assert len(calls) == 2
    assert runtime._ds_reference_latent_cache.entry is None


def test_failure_and_file_change_during_encode_cannot_leave_cached_output(tmp_path):
    torch, runtime, req, calls, loader = setup(tmp_path)
    cache = reference_cache.ReferenceLatentCache(torch)
    cache.load(loader, runtime, req=req, batch_size=1, messages=[])
    def failure(*args, **kwargs):
        raise RuntimeError("failed")
    req.max_ref_seconds = 5
    with pytest.raises(RuntimeError, match="failed"):
        cache.load(failure, runtime, req=req, batch_size=1, messages=[])
    assert cache.entry is None
    def changed(*args, **kwargs):
        (tmp_path / "reference.wav").write_bytes(b"changed")
        return loader(*args, **kwargs)
    cache.load(changed, runtime, req=req, batch_size=1, messages=[])
    assert cache.entry is None


def test_output_capacity_and_runtime_lifetime_are_bounded(tmp_path, monkeypatch):
    torch, runtime, req, calls, loader = setup(tmp_path)
    load = reference_cache.wrap_reference_loader(torch, loader)
    load(runtime, req=req, batch_size=1, messages=[])
    other = SimpleNamespace(**{k: v for k, v in vars(runtime).items() if not k.startswith("_ds")})
    assert load(other, req=req, batch_size=1, messages=[])[0].value == 2
    assert runtime._ds_reference_latent_cache is not other._ds_reference_latent_cache
    monkeypatch.setattr(reference_cache, "MAX_LATENT_BYTES", 1)
    req.max_ref_seconds = 5
    load(runtime, req=req, batch_size=1, messages=[])
    assert runtime._ds_reference_latent_cache.entry is None


@pytest.mark.parametrize("source_matches,cuda", [(False, True), (True, False)])
def test_install_rejects_unknown_source_or_missing_cuda(monkeypatch, source_matches, cuda):
    runtime = SimpleNamespace(InferenceRuntime=SimpleNamespace(_load_reference_latent=lambda: None))
    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: cuda))
    monkeypatch.setattr(reference_cache.importlib, "import_module",
                        lambda name: torch if name == "torch" else runtime)
    monkeypatch.setattr(reference_cache.inspect, "getsource", lambda _: "fixed source")
    if source_matches:
        monkeypatch.setattr(reference_cache, "REFERENCE_SOURCE_SHA256",
                            reference_cache.hashlib.sha256(b"fixed source").hexdigest())
    expected = "cuda_required" if source_matches else "upstream_mismatch"
    with pytest.raises(RuntimeError, match=expected):
        reference_cache.install_reference_latent_cache()


@pytest.mark.parametrize("change", ["dtype", "dimensions", "default_maximum", "codec", "voice"])
def test_runtime_and_voice_changes_invalidate_entry(tmp_path, change):
    torch, runtime, req, calls, loader = setup(tmp_path)
    load = reference_cache.wrap_reference_loader(torch, loader)
    load(runtime, req=req, batch_size=1, messages=[])
    if change == "dtype":
        runtime.model.parameters = lambda: iter([SimpleNamespace(dtype="fp32")])
    elif change == "dimensions":
        runtime.model_cfg.latent_patch_size = 4
    elif change == "default_maximum":
        runtime.default_max_ref_seconds = 10.0
    elif change == "codec":
        runtime.codec = SimpleNamespace(
            model=SimpleNamespace(training=False, hop_length=640), sample_rate=48000,
        )
    else:
        second = tmp_path / "second.wav"
        second.write_bytes(b"second")
        req.ref_wav = str(second)
    assert load(runtime, req=req, batch_size=1, messages=[])[0].value == 2
    assert len(calls) == 2
