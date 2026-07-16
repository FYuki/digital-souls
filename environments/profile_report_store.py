from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from profile_constants import LEGACY_REPORT_ENV, PROFILE_REPORT_ENV
from profile_types import LegacyBackendReport, ProfileError, ResolvedReport


def resolve_report_paths(
    argument: str | None,
    env: dict[str, str],
    default_report: str | None,
) -> tuple[Path, Path]:
    if argument is not None:
        report_path = Path(argument)
    elif PROFILE_REPORT_ENV in env:
        if not env[PROFILE_REPORT_ENV]:
            raise ProfileError(f"{PROFILE_REPORT_ENV} must not be empty")
        report_path = Path(env[PROFILE_REPORT_ENV])
    elif LEGACY_REPORT_ENV in env:
        if not env[LEGACY_REPORT_ENV]:
            raise ProfileError(f"{LEGACY_REPORT_ENV} must not be empty")
        report_path = Path(env[LEGACY_REPORT_ENV]).parent / "resolved-profile.json"
    elif default_report is not None:
        report_path = Path(default_report)
    else:
        raise ProfileError("resolve requires --report or DS_PROFILE_REPORT")

    legacy_path = (
        Path(env[LEGACY_REPORT_ENV])
        if LEGACY_REPORT_ENV in env
        else report_path.parent / "voice-chat-backend.json"
    )
    if LEGACY_REPORT_ENV in env and not env[LEGACY_REPORT_ENV]:
        raise ProfileError(f"{LEGACY_REPORT_ENV} must not be empty")
    try:
        report_path = report_path.resolve()
        legacy_path = legacy_path.resolve()
    except (OSError, RuntimeError) as error:
        raise ProfileError(f"report path cannot be resolved: {error}") from error
    if report_path == legacy_path:
        raise ProfileError(f"resolved report and legacy backend report use the same path: {legacy_path}")
    return report_path, legacy_path


def _prepare_json(path: Path, value: object) -> Path:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output:
            temporary_path = Path(output.name)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        prepared_path = temporary_path
        temporary_path = None
        return prepared_path
    except OSError as error:
        raise ProfileError(f"report cannot be written at {path}: {error.strerror or error}") from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _prepare_backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    temporary_path: Path | None = None
    try:
        with path.open("rb") as source, tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.backup.", delete=False
        ) as output:
            temporary_path = Path(output.name)
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        backup_path = temporary_path
        temporary_path = None
        return backup_path
    except OSError as error:
        raise ProfileError(f"report cannot be backed up at {path}: {error.strerror or error}") from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _replace_prepared(path: Path, temporary_path: Path) -> None:
    try:
        os.replace(temporary_path, path)
    except OSError as error:
        raise ProfileError(f"report cannot be written at {path}: {error.strerror or error}") from error


def _restore_legacy_report(path: Path, backup_path: Path | None) -> None:
    try:
        if backup_path is None:
            path.unlink()
        else:
            os.replace(backup_path, path)
    except OSError as error:
        raise ProfileError(f"legacy report cannot be restored at {path}: {error.strerror or error}") from error


def write_reports(
    report_path: Path,
    report: ResolvedReport,
    legacy_path: Path,
    legacy_report: LegacyBackendReport,
) -> None:
    report_temporary = _prepare_json(report_path, report)
    legacy_temporary: Path | None = None
    legacy_backup: Path | None = None
    try:
        legacy_temporary = _prepare_json(legacy_path, legacy_report)
        legacy_backup = _prepare_backup(legacy_path)
        _replace_prepared(legacy_path, legacy_temporary)
        legacy_temporary = None
        try:
            _replace_prepared(report_path, report_temporary)
        except ProfileError:
            try:
                _restore_legacy_report(legacy_path, legacy_backup)
            except ProfileError:
                legacy_backup = None
                raise
            legacy_backup = None
            raise
    finally:
        for temporary_path in (report_temporary, legacy_temporary, legacy_backup):
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
