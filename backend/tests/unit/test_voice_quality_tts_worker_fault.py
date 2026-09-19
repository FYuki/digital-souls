"""専用TTS以外への故障注入を拒否する境界を検証する。"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "scripts/voice_quality/tts_worker_fault.py"
spec = importlib.util.spec_from_file_location("tts_worker_fault_test", SOURCE)
fault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fault)


def fixture():
    target = {"owner": "voice-quality-423-recovery-test-01", "container_id": "a" * 64,
              "image": "sha256:" + "b" * 64, "base_url": "http://127.0.0.1:50026"}
    container = {"Id": target["container_id"], "Image": target["image"],
                 "Name": "/ds-" + target["owner"], "State": {"Running": True},
                 "HostConfig": {"NetworkMode": "host"},
                 "Config": {"Labels": {"digital-souls.owner": target["owner"]},
                            "Cmd": ["--host", "127.0.0.1", "--port", "50026", "--workers", "1", "--no-access-log"]}}
    return target, container


@pytest.mark.parametrize("change", [
    "owner", "measurement_owner", "short_id", "tag", "shared_port", "container", "image", "name",
    "stopped", "label", "network", "command",
])
def test_refuses_non_owned_or_changed_targets(change):
    target, container = fixture()
    if change == "owner": target["owner"] = "shared-irodori"
    elif change == "measurement_owner": target["owner"] = "voice-quality-423-reference-cache-01"
    elif change == "short_id": target["container_id"] = "abc123"
    elif change == "tag": target["image"] = "irodori:latest"
    elif change == "shared_port": target["base_url"] = "http://127.0.0.1:50024"
    elif change == "container": container["Id"] = "c" * 64
    elif change == "image": container["Image"] = "sha256:" + "c" * 64
    elif change == "name": container["Name"] = "/digital-souls-irodori"
    elif change == "stopped": container["State"]["Running"] = False
    elif change == "label": container["Config"]["Labels"] = {}
    elif change == "network": container["HostConfig"]["NetworkMode"] = "bridge"
    else: container["Config"]["Cmd"][3] = "50024"
    with pytest.raises(ValueError):
        fault.validate_target(target, container)


def test_checks_identity_again_before_worker_signal(monkeypatch):
    target, container = fixture()
    calls = []
    def docker(args, **kwargs):
        calls.append(args)
        assert args[:2] == ["docker", "inspect"]
        current = copy.deepcopy(container)
        if len(calls) == 2:
            current["Config"]["Labels"] = {}
        return subprocess.CompletedProcess(args, 0, json.dumps([current]), "")
    monkeypatch.setattr(fault.subprocess, "run", docker)
    class Opener:
        def open(self, *_args, **_kwargs):
            return io.BytesIO(b'{"active":1}')
    monkeypatch.setattr(fault, "build_opener", lambda _: Opener())
    with pytest.raises(ValueError):
        fault.run(target)
    assert len(calls) == 2


def test_only_exact_verified_container_receives_signal(monkeypatch):
    target, container = fixture()
    calls = []
    def docker(args, **kwargs):
        calls.append(args)
        if args[1] == "inspect":
            return subprocess.CompletedProcess(args, 0, json.dumps([container]), "")
        assert args[:5] == ["docker", "exec", target["container_id"], "/app/.venv/bin/python", "-c"]
        assert args[5] == fault.KILL_WORKER
        return subprocess.CompletedProcess(args, 0, '{"worker_pid":99,"signal":"SIGTERM"}', "")
    monkeypatch.setattr(fault.subprocess, "run", docker)
    class Opener:
        def open(self, *_args, **_kwargs):
            return io.BytesIO(b'{"active":1}')
    monkeypatch.setattr(fault, "build_opener", lambda _: Opener())
    result = fault.run(target)
    assert result["active_observed"] == 1 and result["signal"] == "SIGTERM"
    assert [x[1] for x in calls] == ["inspect", "inspect", "exec"]
    compile(fault.KILL_WORKER, "<worker signal>", "exec")


def test_rejects_arbitrary_docker_distribution_before_any_command(monkeypatch):
    target, _ = fixture()
    target["docker_distro"] = "unrelated"
    monkeypatch.setattr(fault.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="distribution"):
        fault.run(target)


@pytest.mark.parametrize("identity", [None, "", "abc123", "--help", "a" * 63, "a" * 65, "A" * 64, 123, ["a" * 64]])
def test_rejects_invalid_identity_before_docker(monkeypatch, identity):
    target, _ = fixture()
    target["container_id"] = identity
    monkeypatch.setattr(fault.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="exact container identity"):
        fault.run(target)


def test_rejects_missing_identity_before_docker(monkeypatch):
    target, _ = fixture()
    del target["container_id"]
    monkeypatch.setattr(fault.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="exact container identity"):
        fault.run(target)
