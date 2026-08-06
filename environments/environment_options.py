from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from environment_constants import PROFILE_REPORT_ENV, RUN_REPORT_ENV
from profile_types import ProfileError
from app.runtime_paths import RuntimePaths


@dataclass(frozen=True)
class EnvironmentOutputPaths:
    run_report: Path
    profile_report: Path
    legacy_report: Path


def resolve_output_paths(
    *,
    run_report_argument: str | None,
    profile_report_argument: str | None,
    environment: Mapping[str, str],
    run_id: str,
    runtime_paths: RuntimePaths,
) -> EnvironmentOutputPaths:
    run_report = _configured_path(
        run_report_argument, environment.get(RUN_REPORT_ENV), RUN_REPORT_ENV
    )
    if run_report is None:
        run_report = (
            runtime_paths.runtime_report_dir / run_id / "environment-run.json"
        )
    profile_report = _configured_path(
        profile_report_argument, environment.get(PROFILE_REPORT_ENV), PROFILE_REPORT_ENV
    )
    if profile_report is None:
        profile_report = run_report.parent / "resolved-profile.json"
    paths = EnvironmentOutputPaths(
        run_report,
        profile_report,
        profile_report.parent / "voice-chat-backend.json",
    )
    paths = _canonical_output_paths(paths, runtime_paths)
    _validate_distinct(paths)
    return paths


def resolve_existing_run_report_path(
    configured_path: str, runtime_paths: RuntimePaths
) -> Path:
    report_path = Path(configured_path).resolve()
    runtime_report_dir = _canonical_runtime_report_dir(runtime_paths)
    if not report_path.is_relative_to(runtime_report_dir):
        raise ProfileError("environment run report must be inside runtime directory")
    return report_path


def _configured_path(
    argument: str | None, environment_value: str | None, name: str
) -> Path | None:
    configured = argument if argument is not None else environment_value
    if configured is None:
        return None
    if not configured:
        raise ProfileError(f"{name} must not be empty")
    return Path(configured).resolve()


def _validate_distinct(paths: EnvironmentOutputPaths) -> None:
    labelled = {
        "environment run report": paths.run_report,
        "resolved Profile report": paths.profile_report,
        "legacy Backend report": paths.legacy_report,
    }
    if len(set(labelled.values())) != len(labelled):
        rendered = ", ".join(f"{name}={path}" for name, path in labelled.items())
        raise ProfileError(f"environment output paths must be distinct: {rendered}")


def _canonical_output_paths(
    paths: EnvironmentOutputPaths, runtime_paths: RuntimePaths
) -> EnvironmentOutputPaths:
    runtime_report_dir = _canonical_runtime_report_dir(runtime_paths)
    canonical_paths = EnvironmentOutputPaths(
        paths.run_report.resolve(),
        paths.profile_report.resolve(),
        paths.legacy_report.resolve(),
    )
    for path in (
        canonical_paths.run_report,
        canonical_paths.profile_report,
        canonical_paths.legacy_report,
    ):
        if not path.is_relative_to(runtime_report_dir):
            raise ProfileError("environment output path must be inside runtime directory")
    return canonical_paths


def _canonical_runtime_report_dir(runtime_paths: RuntimePaths) -> Path:
    runtime_report_dir = runtime_paths.runtime_report_dir
    if runtime_report_dir.is_symlink():
        raise ProfileError("runtime directory must not be a symlink")
    canonical_root = runtime_paths.data_root.resolve(strict=False)
    canonical_runtime_dir = runtime_report_dir.resolve(strict=False)
    if not canonical_runtime_dir.is_relative_to(canonical_root):
        raise ProfileError("runtime directory must be inside the data root")
    return canonical_runtime_dir
