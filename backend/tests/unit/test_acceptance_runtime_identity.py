"""実接続証跡が可変タグではなく稼働中のimage IDを採取することを検証する。"""

import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def identity():
    return runpy.run_path(str(ROOT / "scripts/acceptance_tool_use.py"))["voicevox_runtime_identity"]


def test_voicevox_identity_reads_only_running_container_image(identity, monkeypatch):
    calls = []
    def inspect(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "true sha256:" + "a" * 64 + "\n", "")
    monkeypatch.setattr(subprocess, "run", inspect)
    result = identity({"ACCEPTANCE_VOICEVOX_CONTAINER": "voicevox", "ACCEPTANCE_VOICEVOX_WSL_DISTRIBUTION": "Ubuntu-dogfood"})
    assert result == {"imageId": "sha256:" + "a" * 64, "source": "running-container"}
    assert calls[0][1:] == ["--distribution", "Ubuntu-dogfood", "--cd", "/tmp", "--exec", "docker", "inspect", "--type=container", "--format", "{{.State.Running}} {{.Image}}", "voicevox"]


@pytest.mark.parametrize("output", ["false sha256:" + "a" * 64, "true latest", ""])
def test_voicevox_identity_rejects_stopped_or_unverifiable_image(identity, monkeypatch, output):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, output, ""))
    with pytest.raises(RuntimeError, match="image ID"):
        identity({"ACCEPTANCE_VOICEVOX_CONTAINER": "voicevox"})


def test_remote_service_without_container_access_is_not_given_a_fabricated_identity(identity, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("コンテナが未指定ならDockerへアクセスしない")
    monkeypatch.setattr(subprocess, "run", unexpected)
    assert identity({}) is None
