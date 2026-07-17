from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from environment_constants import PROFILE_REPORT_ENV, RUN_REPORT_ENV
from profile_types import ProfileError


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
    default_runtime_dir: Path,
) -> EnvironmentOutputPaths:
    run_report = _configured_path(
        run_report_argument, environment.get(RUN_REPORT_ENV), RUN_REPORT_ENV
    )
    if run_report is None:
        run_report = (default_runtime_dir / run_id / "environment-run.json").resolve()
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
    _validate_distinct(paths)
    return paths


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
