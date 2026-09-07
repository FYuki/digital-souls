"""所有する測定用BackendのCPU・メモリと、共有host GPUの数値診断。"""
from __future__ import annotations

import http.client
import json
import math
from pathlib import Path
import re
import socket
import time
from typing import Any, TypeGuard

from app.voice_metrics import DiagnosticValue, ResourceCollection, ResourceMetadata


class _DockerConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(.5)
        self.sock.connect("/var/run/docker.sock")


def _counter(value: object) -> TypeGuard[int]:
    return type(value) is int and value >= 0


class ContainerResourceSampler:
    def __init__(self, report_path: Path) -> None:
        self._report_path = report_path
        self._container_id: str | None = None

    def sample(self) -> dict[str, Any]:
        started = time.monotonic_ns()
        try:
            report = json.loads(self._report_path.read_text())
            backend = report["services"]["backend"]
            identity = backend.get("containerIdentity")
            if backend.get("owned") is not True or not isinstance(identity, dict):
                return {"status": "missing", "reason": "owned_backend_not_running"}
            profile, runtime = report.get("effectiveProfile"), report.get("runtime")
            if (not isinstance(profile, dict) or profile.get("effectiveProfile") not in {"integration-voice", "integration-voice-fault"}
                    or not isinstance(runtime, dict) or runtime.get("environmentId") != "test"
                    or runtime.get("dataRoot") != str(self._report_path.resolve().parents[2])):
                return {"status": "missing", "reason": "resource_profile_mismatch"}
            container = identity.get("containerId")
            if not isinstance(container, str) or not re.fullmatch(r"[a-f0-9]{64}", container):
                return {"status": "missing", "reason": "invalid_container_identity"}
            if self._container_id is not None and container != self._container_id:
                return {"status": "missing", "reason": "backend_container_changed"}
            self._container_id = container
            connection = _DockerConnection("localhost", timeout=.5)
            try:
                connection.request("GET", f"/containers/{container}/stats?stream=false&one-shot=true")
                response = connection.getresponse()
                if response.status != 200:
                    return {"status": "missing", "reason": "container_stats_unavailable"}
                body = response.read(1_000_001)
                if len(body) > 1_000_000:
                    return {"status": "missing", "reason": "container_stats_invalid"}
                raw = json.loads(body)
            finally:
                connection.close()
            cpu = raw["cpu_stats"]["cpu_usage"]["total_usage"]
            memory = raw["memory_stats"]["usage"]
            if raw.get("id") != container or not _counter(cpu) or not _counter(memory):
                return {"status": "missing", "reason": "container_stats_invalid"}
            return {"status": "measured", "scope": "owned_backend_container",
                    "started_ns": started, "completed_ns": time.monotonic_ns(),
                    "cpu_total_ns": cpu, "memory_bytes": memory}
        except FileNotFoundError:
            return {"status": "missing", "reason": "owned_backend_not_running"}
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            # Dockerの例外本文・ID・接続先は出さない。
            return {"status": "missing", "reason": "container_observation_failed"}


def aggregate_resources(rows: list[dict[str, Any]]) -> ResourceMetadata:
    previous: dict[str, Any] | None = None
    cpu_delta = elapsed_ns = intervals = observed = 0
    memory: list[int] = []
    gpu_busy: list[float] = []
    gpu_memory: list[int] = []
    missing: dict[str, int] = {}
    maximum_gap_ns = 0
    for row in rows:
        sample = row.get("backend")
        if not isinstance(sample, dict):
            sample = {"status": "missing", "reason": "backend_observation_absent"}
        if sample.get("status") == "measured":
            names = ("started_ns", "completed_ns", "cpu_total_ns", "memory_bytes")
            if (sample.get("scope") != "owned_backend_container"
                    or not all(_counter(sample.get(name)) for name in names)
                    or sample["completed_ns"] < sample["started_ns"]):
                raise ValueError("invalid backend resource observation")
            observed += 1
            memory.append(sample["memory_bytes"])
            if previous is not None:
                elapsed = sample["completed_ns"] - previous["completed_ns"]
                cpu = sample["cpu_total_ns"] - previous["cpu_total_ns"]
                if elapsed <= 0 or cpu < 0:
                    raise ValueError("backend resource counter regressed")
                cpu_delta += cpu
                elapsed_ns += elapsed
                maximum_gap_ns = max(maximum_gap_ns, elapsed)
                intervals += 1
            previous = sample
        else:
            allowed = {"owned_backend_not_running", "invalid_container_identity", "backend_container_changed",
                       "container_stats_unavailable", "container_stats_invalid", "container_observation_failed", "backend_observation_absent", "resource_profile_mismatch"}
            reason = sample.get("reason")
            if sample.get("status") != "missing" or reason not in allowed:
                raise ValueError("invalid backend resource missing reason")
            missing[reason] = missing.get(reason, 0) + 1
            previous = None
        gpu = row.get("gpu")
        if gpu is None:
            continue  # GPUは低頻度。未サンプリングと失敗を分ける。
        if not isinstance(gpu, dict):
            raise ValueError("invalid GPU observation")
        if gpu.get("outcome") == "observed":
            devices = gpu.get("devices")
            if gpu.get("scope") != "host_gpu" or not isinstance(devices, list) or not devices:
                raise ValueError("invalid GPU observation")
            values: list[float] = []
            sizes: list[int] = []
            for device in devices:
                if not isinstance(device, dict):
                    raise ValueError("invalid GPU device")
                busy, used, total = device.get("utilization_percent"), device.get("used_bytes"), device.get("total_bytes")
                if (isinstance(busy, bool) or not isinstance(busy, (int, float)) or not math.isfinite(busy) or not 0 <= busy <= 100
                        or not _counter(used) or not _counter(total) or used > total):
                    raise ValueError("invalid GPU counters")
                values.append(busy)
                sizes.append(used)
            gpu_busy.append(max(values))
            gpu_memory.append(sum(sizes))
        elif gpu.get("outcome") == "missing" and gpu.get("reason") in {"gpu_values_unavailable", "gpu_probe_failed"}:
            reason = gpu["reason"]
            missing[reason] = missing.get(reason, 0) + 1
        else:
            raise ValueError("invalid GPU missing reason")
    def value(number: float | int | None, reason: str) -> DiagnosticValue:
        return DiagnosticValue(status="missing", reason=reason) if number is None else DiagnosticValue(status="measured", value=number)
    return ResourceMetadata(
        cpu_percent=value(100 * cpu_delta / elapsed_ns if intervals else None, "backend_cpu_interval_unavailable"),
        memory_bytes=value(max(memory) if memory else None, "backend_memory_unavailable"),
        gpu_utilization_percent=value(max(gpu_busy) if gpu_busy else None, "gpu_not_observed"),
        gpu_memory_bytes=value(max(gpu_memory) if gpu_memory else None, "gpu_not_observed"),
        collection=ResourceCollection(backend_samples=observed, cpu_intervals=intervals,
                                     cpu_observed_ms=elapsed_ns / 1e6, maximum_interval_ms=maximum_gap_ns / 1e6,
                                     gpu_samples=len(gpu_busy), missing_samples=missing),
    )
