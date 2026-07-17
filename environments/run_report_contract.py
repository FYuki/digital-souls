from __future__ import annotations


REPORT_FIELDS = {
    "schemaVersion", "runId", "startedAt", "readyAt", "endedAt",
    "resolvedProfilePath", "orchestratorIdentity", "effectiveProfile", "phase", "status",
    "startSequence", "services", "testResult", "failure", "teardown",
}
STATES = {
    "pending", "started", "reused", "external", "in_process", "browser", "disabled",
}
PHASES = {
    "resolve", "verify", "prepare", "pre_probe", "start_or_reuse",
    "readiness", "ready", "supervise", "cleanup", "complete",
}
PRE_READY_PHASES = {
    "resolve", "verify", "prepare", "pre_probe", "start_or_reuse", "readiness",
}
STATUSES = {"running", "ready", "completed", "failed"}
ALLOWED_STATUSES_BY_PHASE = {
    "resolve": {"running", "failed"},
    "verify": {"running", "failed"},
    "prepare": {"running", "failed"},
    "pre_probe": {"running", "failed"},
    "start_or_reuse": {"running", "failed"},
    "readiness": {"running", "failed"},
    "ready": {"ready", "failed"},
    "supervise": {"ready", "failed"},
    "cleanup": {"running", "ready", "failed"},
    "complete": {"completed", "failed"},
}
FAILURE_CATEGORIES = {
    "profile", "preparation", "startup", "readiness", "supervision", "test", "teardown",
}
MODES = {"real", "mock", "disabled", None}
SOURCES = {"managed", "external", "in_process", "browser", None}


class RunReportError(ValueError):
    __slots__ = ()
