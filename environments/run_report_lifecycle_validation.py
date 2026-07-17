from __future__ import annotations

from datetime import datetime
from typing import Mapping

from run_report_contract import (
    ALLOWED_STATUSES_BY_PHASE,
    PRE_READY_PHASES,
    RunReportError,
)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))


def _validate_completion_state(report: Mapping[str, object]) -> None:
    complete = report["phase"] == "complete"
    status = report["status"]
    teardown = report["teardown"]
    if not isinstance(teardown, dict):
        raise RunReportError("invalid teardown")
    if complete != (report["endedAt"] is not None):
        raise RunReportError("lifecycle phase and endedAt must agree")
    if complete:
        if status not in {"completed", "failed"} or teardown["status"] == "pending":
            raise RunReportError("complete lifecycle has invalid final state")
    elif status == "completed" or teardown["status"] != "pending" or teardown["results"]:
        raise RunReportError("incomplete lifecycle has final teardown state")
    if teardown["status"] == "failed" and status != "failed":
        raise RunReportError("failed teardown requires failed lifecycle status")


def _validate_timestamps(report: Mapping[str, object]) -> None:
    phase = report["phase"]
    status = report["status"]
    ready_at = _parse_datetime(report["readyAt"])
    started_at = _parse_datetime(report["startedAt"])
    ended_at = _parse_datetime(report["endedAt"])
    if phase in PRE_READY_PHASES and ready_at is not None:
        raise RunReportError("pre-ready lifecycle cannot have readyAt")
    if (phase in {"ready", "supervise"} or status in {"ready", "completed"}) and ready_at is None:
        raise RunReportError("ready lifecycle requires readyAt")
    if ready_at is not None and started_at is not None and ready_at < started_at:
        raise RunReportError("readyAt cannot precede startedAt")
    if ended_at is not None and started_at is not None and ended_at < started_at:
        raise RunReportError("endedAt cannot precede startedAt")
    if ended_at is not None and ready_at is not None and ended_at < ready_at:
        raise RunReportError("endedAt cannot precede readyAt")


def _validate_test_result_state(report: Mapping[str, object]) -> None:
    test_result = report["testResult"]
    failure = report["failure"]
    if isinstance(test_result, dict) and test_result["status"] == "failed":
        if report["status"] != "failed" or failure is None:
            raise RunReportError(
                "failed testResult requires failed lifecycle status and failure"
            )
    if isinstance(failure, dict) and failure["category"] == "test":
        if not isinstance(test_result, dict) or test_result["status"] != "failed":
            raise RunReportError("test failure requires failed testResult")


def validate_run_report_lifecycle(report: Mapping[str, object]) -> None:
    status = report["status"]
    if (status == "failed") != (report["failure"] is not None):
        raise RunReportError("lifecycle status and failure must agree")
    _validate_test_result_state(report)
    _validate_completion_state(report)
    phase = report["phase"]
    if not isinstance(phase, str) or status not in ALLOWED_STATUSES_BY_PHASE[phase]:
        raise RunReportError("lifecycle phase and status must agree")
    _validate_timestamps(report)
