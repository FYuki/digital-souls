from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path

from adapters.base import AdapterOperationError
from commands.profile_command import resolve_and_write_profile
from environment_options import resolve_output_paths
from environment_constants import (
    DEFAULT_READY_GATE_URL,
    READY_GATE_ENV,
    RUN_REPORT_CLEANUP_TARGET,
)
from environment_runtime import (
    EnvironmentRun,
    SupervisionError,
)
from environment_signals import (
    defer_interrupt_signals,
    install_interrupt_handlers,
    restore_interrupt_handlers,
)
from environment_verification import EnvironmentVerificationError
from orchestrator import classify_failure
from run_report import create_initial_report, create_pending_report, record_cleanup, record_failure
from run_report_store import RunReportStore
from run_report_timestamps import current_timestamp, next_lifecycle_timestamp


def up_environment(
    root_dir: Path, default_runtime_dir: Path, arguments: argparse.Namespace
) -> int:
    run_id = str(uuid.uuid4())
    started_at = current_timestamp()
    paths = resolve_output_paths(
        run_report_argument=arguments.run_report,
        profile_report_argument=arguments.profile_report,
        environment=os.environ,
        run_id=run_id,
        default_runtime_dir=default_runtime_dir,
    )
    configured_ready_gate = os.environ.get(READY_GATE_ENV)
    ready_gate_url = (
        DEFAULT_READY_GATE_URL
        if not configured_ready_gate
        else configured_ready_gate
    )
    store = RunReportStore(paths.run_report)
    report = create_pending_report(
        run_id=run_id,
        started_at=started_at,
        resolved_profile_path=paths.profile_report,
    )
    store.save(report)
    phase = "resolve"
    environment_run: EnvironmentRun | None = None
    was_interrupted, previous_handlers = install_interrupt_handlers()
    try:
        profile = resolve_and_write_profile(
            dict(os.environ),
            arguments.default_profile,
            paths.profile_report,
            paths.legacy_report,
        )
        report = create_initial_report(
            run_id=run_id,
            started_at=started_at,
            resolved_profile_path=paths.profile_report,
            effective_profile=profile,
        )
        store.save(report)
        environment_run = EnvironmentRun(
            profile=profile,
            profile_path=paths.profile_report,
            store=store,
            report=report,
            root_dir=root_dir,
            ready_gate_url=ready_gate_url,
        )
        phase = "verify"
        environment_run.verify()
        phase = "prepare"
        environment_run.prepare()
        phase = "pre_probe"
        decisions = environment_run.pre_probe()
        phase = "start_or_reuse"
        environment_run.start_or_reuse(decisions)
        phase = "readiness"
        environment_run.wait_until_ready()
        phase = "supervise"
        environment_run.begin_supervision()
        environment_run.supervise()
    except (Exception, KeyboardInterrupt) as error:
        current = environment_run.report if environment_run is not None else report
        if not (was_interrupted() and current.get("status") == "ready"):
            try:
                store.update(
                    lambda stored: (
                        stored
                        if stored.get("failure") is not None
                        else record_failure(
                            stored,
                            category=_failure_category(error, phase),
                            message=str(error),
                        )
                    )
                )
            except Exception as report_error:
                if environment_run is not None:
                    environment_run.cleanup_failures.append(
                        {
                            "service": RUN_REPORT_CLEANUP_TARGET,
                            "result": "failed",
                            "message": f"run report update failed: {report_error}",
                        }
                    )
    finally:
        try:
            with defer_interrupt_signals():
                if environment_run is not None:
                    cleanup_results = environment_run.cleanup()
                else:
                    cleanup_results = []
                final_report = store.update(
                    lambda report: record_cleanup(
                        report,
                        results=cleanup_results,
                        ended_at=next_lifecycle_timestamp(report),
                    )
                )
        finally:
            restore_interrupt_handlers(previous_handlers)
    return 0 if final_report["status"] == "completed" else 1


def _failure_category(error: BaseException, phase: str) -> str:
    if isinstance(error, EnvironmentVerificationError):
        return error.category
    if isinstance(error, SupervisionError):
        return "supervision"
    if isinstance(error, AdapterOperationError):
        return error.category
    return classify_failure(phase)["category"]
