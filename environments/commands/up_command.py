from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path

from adapters.base import AdapterOperationError
from commands.profile_command import resolve_and_write_profile
from environment_options import resolve_output_paths
from environment_constants import RUN_REPORT_CLEANUP_TARGET
from environment_runtime import (
    EnvironmentRun,
    SupervisionError,
)
from environment_signals import (
    block_interrupt_signals,
    coalesce_interrupt_signals,
    defer_interrupt_signals,
    install_interrupt_handlers,
    restore_interrupt_handlers,
)
from environment_timing import EnvironmentTiming
from environment_verification import EnvironmentVerificationError
from orchestrator import classify_failure
from process_control import current_process_identity
from run_report import create_initial_report, create_pending_report, record_cleanup, record_failure
from run_report_store import RunReportStore
from run_report_timestamps import current_timestamp, next_lifecycle_timestamp
from service_registry import ServiceRegistry, create_service_registry
from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import resolve_runtime_paths, runtime_paths_projection


def up_environment(
    root_dir: Path,
    arguments: argparse.Namespace,
    *,
    registry: ServiceRegistry | None = None,
    timing: EnvironmentTiming | None = None,
) -> int:
    resolved_timing = timing if timing is not None else EnvironmentTiming()
    run_id = str(uuid.uuid4())
    started_at = current_timestamp()
    runtime_paths = resolve_runtime_paths(os.environ, root_dir)
    initialize_runtime_data_root(runtime_paths, root_dir)
    paths = resolve_output_paths(
        run_report_argument=arguments.run_report,
        profile_report_argument=arguments.profile_report,
        environment=os.environ,
        run_id=run_id,
        runtime_paths=runtime_paths,
    )
    store = RunReportStore(paths.run_report)
    phase = "resolve"
    environment_run: EnvironmentRun | None = None
    previous_handlers = None
    report = None
    try:
        with block_interrupt_signals():
            was_interrupted, previous_handlers = install_interrupt_handlers()
            with defer_interrupt_signals():
                orchestrator_identity = current_process_identity().to_report()
                report = create_pending_report(
                    run_id=run_id,
                    started_at=started_at,
                    resolved_profile_path=paths.profile_report,
                    orchestrator_identity=orchestrator_identity,
                    runtime=runtime_paths_projection(runtime_paths),
                )
                store.save(report)
        paths.profile_report.unlink(missing_ok=True)
        paths.legacy_report.unlink(missing_ok=True)
        profile = resolve_and_write_profile(
            dict(os.environ),
            arguments.default_profile,
            paths.profile_report,
            paths.legacy_report,
            runtime_paths,
        )
        derived = profile.get("derivedEnvironment")
        if not isinstance(derived, dict):
            raise ValueError("resolved derived environment is required")
        dependencies = profile.get("dependencies")
        if not isinstance(dependencies, dict):
            raise ValueError("resolved dependencies are required")
        backend = dependencies.get("backend")
        if not isinstance(backend, dict):
            raise ValueError("resolved backend dependency is required")
        if registry is not None:
            resolved_registry = registry
        elif backend.get("mode") == "real":
            resolved_registry = create_service_registry(
                root_dir,
                runtime_paths,
                ollama_model_name=derived["OLLAMA_CHAT_MODEL"],
                whisper_model_name=derived["WHISPER_MODEL"],
            )
        else:
            resolved_registry = create_service_registry(root_dir, runtime_paths)
        ready_gate = profile.get("readyGate")
        if not isinstance(ready_gate, dict):
            raise ValueError("resolved ready gate is required")
        report = create_initial_report(
            run_id=run_id,
            started_at=started_at,
            resolved_profile_path=paths.profile_report,
            effective_profile=profile,
            orchestrator_identity=orchestrator_identity,
            runtime=runtime_paths_projection(runtime_paths),
        )
        store.save(report)
        environment_run = EnvironmentRun(
            profile=profile,
            profile_path=paths.profile_report,
            store=store,
            report=report,
            ready_gate=ready_gate,
            was_interrupted=was_interrupted,
            registry=resolved_registry,
            timing=resolved_timing,
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
        if report is None:
            raise
        current = environment_run.report if environment_run is not None else report
        if not (was_interrupted() and current.get("status") == "ready"):
            failure_category = _failure_category(error, phase)
            failure_message = str(error)
            try:
                store.update(
                    lambda stored: (
                        stored
                        if stored.get("failure") is not None
                        else record_failure(
                            stored,
                            category=failure_category,
                            message=failure_message,
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
            with coalesce_interrupt_signals():
                if report is not None:
                    cleanup_results = (
                        environment_run.cleanup()
                        if environment_run is not None
                        else []
                    )
                    final_report = store.update(
                        lambda report: record_cleanup(
                            report,
                            results=cleanup_results,
                            ended_at=next_lifecycle_timestamp(report),
                        )
                    )
        finally:
            if previous_handlers is not None:
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
