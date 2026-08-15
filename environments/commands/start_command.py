from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

from app.runtime_paths import resolve_runtime_paths
from environment_options import resolve_output_paths
from process_control import (
    ProcessIdentity,
    process_identity_matches,
    start_managed_process,
)
from run_report_store import RunReportStore

STARTUP_OBSERVATION_INTERVAL_SECONDS = 0.05


def start_environment(root_dir: Path, arguments: argparse.Namespace) -> int:
    runtime_paths = resolve_runtime_paths(os.environ, root_dir)
    paths = resolve_output_paths(
        run_report_argument=arguments.run_report,
        profile_report_argument=arguments.profile_report,
        environment=os.environ,
        run_id=str(uuid.uuid4()),
        runtime_paths=runtime_paths,
    )
    command = [
        sys.executable,
        str(root_dir / "environments" / "environment_cli.py"),
        "up",
        "--run-report",
        str(paths.run_report),
        "--profile-report",
        str(paths.profile_report),
    ]
    if arguments.default_profile is not None:
        command.extend(("--default-profile", arguments.default_profile))
    orchestrator = start_managed_process(
        label="environment orchestrator",
        command=tuple(command),
        cwd=root_dir,
        env=os.environ,
    )
    store = RunReportStore(paths.run_report)

    while True:
        return_code = orchestrator.process.poll()
        if paths.run_report.is_file():
            report = store.load()
            recorded_identity = ProcessIdentity.from_report(
                _require_identity(report.get("orchestratorIdentity"))
            )
            if recorded_identity == orchestrator.identity:
                if report.get("status") == "ready":
                    if return_code is not None or not process_identity_matches(
                        recorded_identity
                    ):
                        raise RuntimeError(
                            "environment orchestrator exited after reporting readiness"
                        )
                    return 0
                if return_code is not None:
                    return return_code if return_code != 0 else 1
        elif return_code is not None:
            return return_code if return_code != 0 else 1
        time.sleep(STARTUP_OBSERVATION_INTERVAL_SECONDS)


def _require_identity(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("orchestratorIdentity must be an object")
    return value
