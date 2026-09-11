"""手動dogfood観測をtraceのhashと照合し、数値だけのresource集計へ取り込む。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.voice_metrics import DiagnosticValue, ManualResourceCollection, ResourceMetadata

NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
_METRICS = ("cpu_percent", "memory_bytes", "gpu_utilization_percent", "gpu_memory_bytes")


class DogfoodResourceSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    elapsed_ms: NonNegativeFloat
    # nullは未観測。省略や0への補完を認めない。
    cpu_percent: NonNegativeFloat | None
    memory_bytes: NonNegativeInt | None
    gpu_utilization_percent: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False, strict=True)] | None
    gpu_memory_bytes: NonNegativeInt | None


class DogfoodResourceObservations(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    measurement_kind: Literal["dogfood"]
    method: Literal["manual_docker_stats_and_host_gpu_v1"]
    trace_sha256: list[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] = Field(min_length=1)
    samples: list[DogfoodResourceSample] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sequence(self) -> DogfoodResourceObservations:
        if len(set(self.trace_sha256)) != len(self.trace_sha256):
            raise ValueError("duplicate trace hash")
        if any(b.elapsed_ms <= a.elapsed_ms for a, b in zip(self.samples, self.samples[1:])):
            raise ValueError("manual resource sample clocks must increase")
        return self


def load_dogfood_resources(path: Path, *, trace_paths: Sequence[Path]) -> ResourceMetadata:
    raw = DogfoodResourceObservations.model_validate_json(path.read_text(encoding="utf-8"))
    actual = [hashlib.sha256(trace.read_bytes()).hexdigest() for trace in trace_paths]
    if sorted(actual) != sorted(raw.trace_sha256):
        raise ValueError("manual resource observations do not match selected traces")
    metrics: dict[str, DiagnosticValue] = {}
    measured: dict[str, int] = {}
    missing: dict[str, int] = {}
    for name in _METRICS:
        values = [getattr(sample, name) for sample in raw.samples if getattr(sample, name) is not None]
        measured[name], missing[name] = len(values), len(raw.samples) - len(values)
        if not values:
            metrics[name] = DiagnosticValue(status="missing", reason="manual_dogfood_sample_not_recorded")
        else:
            value = sum(values) / len(values) if name == "cpu_percent" else max(values)
            metrics[name] = DiagnosticValue(status="measured", value=value)
    collection = ManualResourceCollection(
        sample_count=len(raw.samples),
        sample_window_ms=raw.samples[-1].elapsed_ms - raw.samples[0].elapsed_ms,
        maximum_interval_ms=max((b.elapsed_ms - a.elapsed_ms for a, b in zip(raw.samples, raw.samples[1:])), default=0),
        measured_samples=measured,
        missing_samples=missing,
    )
    return ResourceMetadata(**metrics, collection=collection)
