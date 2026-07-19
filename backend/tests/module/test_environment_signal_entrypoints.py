from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.environment_entrypoint_test_support import (
    ROOT_DIR,
    wait_for_report_phase as _wait_for_report_phase,
)
def test_should_stop_owned_voicevox_after_sigterm_during_single_service_readiness(
    tmp_path: Path,
):
    ready_marker = tmp_path / "readiness-started"
    stop_log = tmp_path / "stop.log"
    harness = ROOT_DIR / "backend" / "tests" / "harnesses" / "voicevox_signal.py"
    process = subprocess.Popen(
        [sys.executable, str(harness), str(ROOT_DIR), str(ready_marker), str(stop_log)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_marker.exists()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 1, (stdout, stderr)
    assert stop_log.read_text(encoding="utf-8").strip() == "owned"


@pytest.mark.parametrize("interrupt_point", ["before_save", "during_save"])
def test_should_block_sigterm_until_orchestrator_identity_is_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interrupt_point: str
):
    import commands.up_command as up_command
    from run_report_store import RunReportStore

    worker_stop = threading.Event()
    worker = threading.Thread(target=worker_stop.wait)
    worker.start()
    if worker.ident is None:
        pytest.fail("worker thread identity was not published")

    class InterruptingReportStore(RunReportStore):
        interrupted = False

        def save(self, report):
            if interrupt_point == "before_save" and not self.interrupted:
                self.interrupted = True
                signal.pthread_kill(worker.ident, signal.SIGTERM)
            super().save(report)

        def _save_unlocked(self, report):
            if interrupt_point == "during_save" and not self.interrupted:
                self.interrupted = True
                signal.pthread_kill(worker.ident, signal.SIGTERM)
            super()._save_unlocked(report)

    monkeypatch.setattr(up_command, "RunReportStore", InterruptingReportStore)
    report_path = tmp_path / "environment-run.json"
    arguments = argparse.Namespace(
        run_report=str(report_path),
        profile_report=None,
        default_profile="test-mocked",
    )

    try:
        exit_code = up_command.up_environment(
            ROOT_DIR, tmp_path / ".runtime", arguments
        )
    finally:
        worker_stop.set()
        worker.join(timeout=5)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["phase"] == "complete"
    assert report["status"] == "failed"
    assert report["endedAt"] is not None
    assert report["orchestratorIdentity"]["pid"] == os.getpid()


def test_should_publish_report_when_sigterm_arrives_during_identity_capture(
    tmp_path: Path,
):
    report_path = tmp_path / "environment-run.json"
    runtime_path = tmp_path / ".runtime"
    harness = (
        ROOT_DIR / "backend" / "tests" / "harnesses" / "identity_capture_signal.py"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(harness),
            str(ROOT_DIR),
            str(report_path),
            str(runtime_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert report_path.exists(), result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result.returncode == 1, result.stderr
    assert report["phase"] == "complete"
    assert report["status"] == "failed"
    assert report["endedAt"] is not None
    assert report["orchestratorIdentity"]["pid"] != os.getpid()


def test_should_delegate_live_down_cleanup_to_up_even_after_additional_sigterm(
    tmp_path: Path, environment_report_validator
):
    stop_log = tmp_path / "stop-order.log"
    report_path = tmp_path / "environment-run.json"
    harness = ROOT_DIR / "backend" / "tests" / "harnesses" / "environment_signal.py"
    environment = {
        **os.environ,
        "DS_PROFILE": "test-mocked",
        "DS_ENVIRONMENT_READY_URL": "http://127.0.0.1:0/ready",
        "TEST_STOP_LOG": str(stop_log),
    }
    process = subprocess.Popen(
        [sys.executable, str(harness), str(ROOT_DIR), str(report_path), str(stop_log)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_report_phase(report_path, "supervise")
        down_result = subprocess.run(
            [str(ROOT_DIR / "environments" / "down.sh"), "--run-report", str(report_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        deadline = time.monotonic() + 10
        while (
            (not stop_log.exists() or not stop_log.read_text(encoding="utf-8"))
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert stop_log.exists()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    environment_report_validator.validate(report)
    assert down_result.returncode == 0, (down_result.stdout, down_result.stderr)
    assert process.returncode == 0, (stdout, stderr)
    assert stop_log.read_text(encoding="utf-8").splitlines() == [
        "begin:frontend",
        "frontend",
        "begin:backend",
        "backend",
    ]
    assert report["phase"] == "complete"
    assert report["status"] == "completed"
    assert report["endedAt"] is not None
    assert report["failure"] is None
    assert report["orchestratorIdentity"]["pid"] == process.pid
    assert report["teardown"] == {
        "status": "completed",
        "results": [
            {"service": "frontend", "result": "stopped_term"},
            {"service": "backend", "result": "stopped_term"},
        ],
    }
