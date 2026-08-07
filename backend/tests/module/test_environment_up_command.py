from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.environment_entrypoint_test_support import (
    ROOT_DIR,
    copy_environment_runtime as _copy_environment_runtime,
)
def test_should_allow_run_report_environment_override_through_up_cli(tmp_path: Path, runtime_paths):
    environments = _copy_environment_runtime(tmp_path)
    report_path = runtime_paths.runtime_report_dir / "custom" / "environment-run.json"
    env = {
        **os.environ,
        "DS_PROFILE": "missing-profile",
        "DS_ENVIRONMENT_RUN_REPORT": str(report_path),
    }

    result = subprocess.run(
        [str(environments / "up.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["failure"]["category"] == "profile"
    assert report["status"] == "failed"


def test_should_remove_previous_profile_report_before_profile_resolution(tmp_path: Path, runtime_paths):
    environments = _copy_environment_runtime(tmp_path)
    report_path = runtime_paths.runtime_report_dir / "custom" / "environment-run.json"
    profile_report_path = report_path.parent / "resolved-profile.json"
    profile_report_path.parent.mkdir(parents=True)
    profile_report_path.write_text('{"runId":"stale-run"}', encoding="utf-8")
    env = {
        **os.environ,
        "DS_PROFILE": "missing-profile",
        "DS_ENVIRONMENT_RUN_REPORT": str(report_path),
    }

    result = subprocess.run(
        [str(environments / "up.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not profile_report_path.exists()


def test_should_record_profile_failure_when_previous_profile_report_cannot_be_removed(
    tmp_path: Path,
    runtime_paths,
):
    environments = _copy_environment_runtime(tmp_path)
    report_path = runtime_paths.runtime_report_dir / "custom" / "environment-run.json"
    profile_report_path = report_path.parent / "resolved-profile.json"
    profile_report_path.mkdir(parents=True)
    env = {
        **os.environ,
        "DS_PROFILE": "test-mocked",
        "DS_ENVIRONMENT_RUN_REPORT": str(report_path),
    }

    result = subprocess.run(
        [str(environments / "up.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["failure"]["category"] == "profile"
    assert report["status"] == "failed"


@pytest.mark.parametrize("configuration_source", ["cli", "environment"])
def test_should_reject_colliding_environment_output_paths_before_first_write(
    configuration_source: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime_paths,
):
    import commands.up_command as up_command
    from profile_types import ProfileError

    collision = runtime_paths.runtime_report_dir / "colliding-report.json"
    monkeypatch.delenv("DS_ENVIRONMENT_RUN_REPORT", raising=False)
    monkeypatch.delenv("DS_PROFILE_REPORT", raising=False)
    if configuration_source == "environment":
        monkeypatch.setenv("DS_ENVIRONMENT_RUN_REPORT", str(collision))
        monkeypatch.setenv("DS_PROFILE_REPORT", str(collision))
        run_report = None
        profile_report = None
    else:
        run_report = str(collision)
        profile_report = str(collision)
    arguments = argparse.Namespace(
        run_report=run_report,
        profile_report=profile_report,
        default_profile="test-mocked",
    )

    with pytest.raises(ProfileError, match="output paths must be distinct"):
        up_command.up_environment(ROOT_DIR, arguments)

    assert not collision.exists()


def test_should_reject_runtime_symlink_before_run_report_store_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import commands.up_command as up_command

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    data_root = tmp_path / "runtime-data"
    data_root.mkdir(exist_ok=True)
    marker = data_root / ".environment-identity.json"
    marker.write_text(
        json.dumps({"schemaVersion": 1, "environmentId": "test"}),
        encoding="utf-8",
    )
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    (data_root / "runtime").symlink_to(external, target_is_directory=True)
    store_creations = 0

    class UnexpectedStore:
        def __init__(self, _path: Path) -> None:
            nonlocal store_creations
            store_creations += 1

    monkeypatch.setenv("DS_ENVIRONMENT_ID", "test")
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))
    monkeypatch.setattr(up_command, "RunReportStore", UnexpectedStore)
    arguments = argparse.Namespace(
        run_report=None,
        profile_report=None,
        default_profile="test-mocked",
    )

    with pytest.raises(ValueError):
        up_command.up_environment(repository_root, arguments)

    assert store_creations == 0
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(external.iterdir()) == [sentinel]


def test_should_finalize_interrupted_ready_run_when_wall_clock_moves_backward(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime_paths
):
    from tests.environment_test_support import resolved_profile

    import commands.up_command as up_command
    import run_report_timestamps
    from run_report import record_ready

    class InterruptedReadyRun:
        def __init__(self, *, report, store, **_options):
            self.report = report
            self.store = store

        def verify(self):
            return None

        def prepare(self):
            return None

        def pre_probe(self):
            return {}

        def start_or_reuse(self, decisions):
            return None

        def wait_until_ready(self):
            self.report = self.store.update(
                lambda report: record_ready(
                    report, ready_at="2026-07-17T00:00:02+00:00"
                )
            )

        def begin_supervision(self):
            return None

        def supervise(self):
            raise InterruptedError("received SIGTERM")

        def cleanup(self):
            return []

    timestamps = iter(
        (
            datetime(2026, 7, 17, 0, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 17, 0, 0, 0, tzinfo=timezone.utc),
        )
    )
    monkeypatch.setattr(
        run_report_timestamps, "_wall_clock_timestamp", lambda: next(timestamps)
    )
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda env, default, path, legacy, runtime: resolved_profile("test-mocked"),
    )
    monkeypatch.setattr(up_command, "EnvironmentRun", InterruptedReadyRun)
    monkeypatch.setattr(
        up_command, "install_interrupt_handlers", lambda: (lambda: True, {})
    )
    monkeypatch.setattr(up_command, "restore_interrupt_handlers", lambda handlers: None)
    report_path = runtime_paths.runtime_report_dir / "clock" / "environment-run.json"
    arguments = argparse.Namespace(
        run_report=str(report_path),
        profile_report=None,
        default_profile="test-mocked",
    )

    exit_code = up_command.up_environment(ROOT_DIR, arguments)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["phase"] == "complete"
    assert report["status"] == "completed"
    assert report["readyAt"] == "2026-07-17T00:00:02+00:00"
    assert report["endedAt"] == "2026-07-17T00:00:02+00:00"
    assert report["failure"] is None


def test_should_ignore_default_environment_runtime_directory():
    runtime_report = ROOT_DIR / ".runtime" / "environments" / "run-001" / "environment-run.json"

    result = subprocess.run(
        ["git", "check-ignore", "--quiet", str(runtime_report)],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
