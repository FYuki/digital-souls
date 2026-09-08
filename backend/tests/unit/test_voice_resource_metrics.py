from __future__ import annotations

import json

import pytest

from app.voice_resource_metrics import ContainerResourceSampler, aggregate_resources


def sample(at: int, cpu: int, memory: int = 100):
    return {"status": "measured", "scope": "owned_backend_container", "started_ns": at - 1,
            "completed_ns": at, "cpu_total_ns": cpu, "memory_bytes": memory}


def test_resource_cpu_uses_deltas_and_memory_uses_sampled_peak():
    result = aggregate_resources([{"backend": sample(1_000_000_000, 100_000_000)},
                                  {"backend": sample(1_500_000_000, 200_000_000, 150)},
                                  {"backend": sample(2_500_000_000, 600_000_000, 125)}])
    assert result.cpu_percent.value == pytest.approx(100 / 3)
    assert result.memory_bytes.value == 150
    assert result.collection.cpu_observed_ms == 1500
    assert result.collection.cpu_intervals == 2
    assert result.gpu_utilization_percent.status == "missing"


def test_missing_resource_samples_break_intervals_and_keep_missing_counts():
    result = aggregate_resources([{"backend": sample(1_000, 100)},
                                  {"backend": {"status": "missing", "reason": "container_observation_failed"}},
                                  {"backend": sample(3_000, 900)}])
    assert result.cpu_percent.status == "missing"
    assert result.collection.missing_samples == {"container_observation_failed": 1}
    assert result.collection.backend_samples == 2


@pytest.mark.parametrize("last", [sample(2000, 50), sample(1000, 200), {**sample(2000, 200), "memory_bytes": -1}])
def test_regressed_or_invalid_resource_samples_are_rejected(last):
    with pytest.raises(ValueError):
        aggregate_resources([{"backend": sample(1000, 100)}, {"backend": last}])


def test_gpu_scope_and_peak_are_distinct_from_backend_memory():
    result = aggregate_resources([{"gpu": {"outcome": "observed", "scope": "host_gpu", "devices": [
        {"utilization_percent": 20, "used_bytes": 100, "total_bytes": 500},
        {"utilization_percent": 40, "used_bytes": 200, "total_bytes": 500},
    ]}}])
    assert result.memory_bytes.status == "missing"
    assert result.gpu_memory_bytes.value == 300
    assert result.gpu_utilization_percent.value == 40
    assert result.collection.gpu_scope == "shared_host_gpu"


def test_resource_sampler_refuses_unowned_container_without_connecting(tmp_path, monkeypatch):
    import app.voice_resource_metrics as module
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"services": {"backend": {"owned": False, "containerIdentity": {"containerId": "a" * 64}}}}))
    monkeypatch.setattr(module, "_DockerConnection", lambda *args, **kwargs: pytest.fail("must not connect"))
    assert ContainerResourceSampler(report).sample() == {"status": "missing", "reason": "owned_backend_not_running"}


@pytest.mark.parametrize("profile_name", ["integration-voice", "integration-voice-fault", "integration-voice-pcm"])
def test_resource_sampler_emits_only_counters_and_checks_container_identity(tmp_path, monkeypatch, profile_name):
    import app.voice_resource_metrics as module
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"effectiveProfile": {"effectiveProfile": profile_name},
                                  "runtime": {"environmentId": "test", "dataRoot": str(report.resolve().parents[2])},
                                  "services": {"backend": {"owned": True, "containerIdentity": {"containerId": "a" * 64}}}}))
    class Connection:
        status = 200
        def __init__(self, *args, **kwargs): pass
        def request(self, method, path):
            assert method == "GET"
            assert path == "/containers/" + "a" * 64 + "/stats?stream=false&one-shot=true"
        def getresponse(self): return self
        def read(self, limit):
            return json.dumps({"id": "a" * 64, "name": "private-container", "cpu_stats": {"cpu_usage": {"total_usage": 123}}, "memory_stats": {"usage": 456}, "secret": "private-value"}).encode()
        def close(self): pass
    monkeypatch.setattr(module, "_DockerConnection", Connection)
    sampler = ContainerResourceSampler(report)
    observed = sampler.sample()
    assert observed["cpu_total_ns"] == 123
    assert observed["memory_bytes"] == 456
    assert "private" not in json.dumps(observed) and "a" * 64 not in json.dumps(observed)
    report.write_text(report.read_text().replace("a" * 64, "b" * 64))
    assert sampler.sample() == {"status": "missing", "reason": "backend_container_changed"}


@pytest.mark.parametrize("wrong", ["environment", "data_root", "profile"])
def test_resource_sampler_rejects_other_environment_before_docker_access(tmp_path, monkeypatch, wrong):
    import app.voice_resource_metrics as module
    report = tmp_path / "report.json"
    body = {"effectiveProfile": {"effectiveProfile": "integration-voice"},
            "runtime": {"environmentId": "test", "dataRoot": str(report.resolve().parents[2])},
            "services": {"backend": {"owned": True, "containerIdentity": {"containerId": "a" * 64}}}}
    if wrong == "environment": body["runtime"]["environmentId"] = "dogfood"
    if wrong == "data_root": body["runtime"]["dataRoot"] = "/other/data"
    if wrong == "profile": body["effectiveProfile"]["effectiveProfile"] = "dev"
    report.write_text(json.dumps(body))
    monkeypatch.setattr(module, "_DockerConnection", lambda *args, **kwargs: pytest.fail("must not connect"))
    assert ContainerResourceSampler(report).sample() == {"status": "missing", "reason": "resource_profile_mismatch"}
