from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[3] / "scripts/voice_quality/native_sdk.py"
_SPEC = importlib.util.spec_from_file_location("native_sdk_sampler", _PATH)
module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(module)


def setup_report(tmp_path, *, owner=True, profile="integration-voice-fault", data_root=None):
    path = tmp_path / "runtime-data/runtime/standalone/environment-run.json"
    path.parent.mkdir(parents=True)
    report = {"runtime": {"environmentId": "test", "dataRoot": data_root or str(tmp_path / "runtime-data")},
              "effectiveProfile": {"effectiveProfile": profile}, "services": {"backend": {
                  "owned": owner, "containerIdentity": {"containerId": "a" * 64}}}}
    path.write_text(json.dumps(report))
    return path, report


@pytest.mark.parametrize("options", [{"owner": False}, {"profile": "dogfood"}, {"data_root": "/other"}])
def test_only_exclusive_test_backend_is_inspected(tmp_path, monkeypatch, options):
    path, _ = setup_report(tmp_path, **options)
    def reject(*args, **kwargs):
        raise AssertionError("対象外のcontainerを操作してはいけない")
    monkeypatch.setattr(module.subprocess, "run", reject)
    assert module.NativeSdkSampler(path).sample() is None


def test_no_sdk_proof_is_invented_before_loading(tmp_path, monkeypatch):
    path, report = setup_report(tmp_path)
    calls = []
    def inspect(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "missing", "reason": "native_sdk_not_loaded"}))
    monkeypatch.setattr(module.subprocess, "run", inspect)
    sampler = module.NativeSdkSampler(path)
    assert sampler.sample() is None
    assert calls[0][2] == "a" * 64
    report["services"]["backend"]["containerIdentity"]["containerId"] = "b" * 64
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="container_changed"):
        sampler.sample()
    assert len(calls) == 1


def test_starting_backend_without_identity_is_not_inspected(tmp_path, monkeypatch):
    path, report = setup_report(tmp_path)
    report["services"]["backend"]["containerIdentity"] = None
    path.write_text(json.dumps(report))
    assert module.NativeSdkSampler(path).sample() is None
