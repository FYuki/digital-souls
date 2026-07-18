from __future__ import annotations

from pathlib import Path

from profile_report import create_legacy_report
from profile_report_store import write_reports
from profile_resolution import resolve_profile


def resolve_and_write_profile(
    environment: dict[str, str],
    default_profile: str | None,
    profile_path: Path,
    legacy_path: Path,
) -> dict[str, object]:
    profile = resolve_profile(environment, default_profile)
    write_reports(profile_path, profile, legacy_path, create_legacy_report(profile))
    return dict(profile)
