from __future__ import annotations

from dataclasses import dataclass

from .models import Json, MCPFailure


@dataclass(frozen=True)
class ClassifiedResult:
    outcome: str
    error_category: str | None = None
    native_error: Json | None = None
    request_ids: frozenset[str] | None = None
    interaction_id: str | None = None
    dispatch_started: bool = False


def classify_native_payload(
    payload: Json,
    *,
    claimed_action: bool = False,
    dispatch_started: bool = False,
) -> ClassifiedResult:
    if payload.get("resultType") != "input_required":
        if payload.get("isError"):
            return ClassifiedResult(
                "result_unknown" if claimed_action and dispatch_started else "failed",
                error_category="tool_error",
                dispatch_started=dispatch_started,
            )
        return ClassifiedResult("succeeded", dispatch_started=dispatch_started)

    request_state = payload.get("requestState")
    requests = payload.get("inputRequests") or {}
    sent_action = claimed_action and dispatch_started
    if (
        request_state is not None and not isinstance(request_state, str)
    ) or not isinstance(requests, dict):
        return ClassifiedResult(
            "result_unknown" if sent_action else "failed",
            error_category="protocol",
            native_error={"code": "invalid_input_required"},
            dispatch_started=dispatch_started,
        )
    if any(
        not isinstance(request, dict)
        or request.get("method") != "elicitation/create"
        for request in requests.values()
    ):
        return ClassifiedResult(
            "result_unknown" if sent_action else "unsupported",
            error_category="policy",
            native_error={"code": "required_capability_unsupported"},
            dispatch_started=dispatch_started,
        )
    return ClassifiedResult(
        "input_required",
        request_ids=frozenset(requests),
        dispatch_started=dispatch_started,
    )


def classify_mcp_failure(
    error: MCPFailure,
    *,
    claimed_action: bool,
    dispatch_started: bool,
) -> ClassifiedResult:
    started = dispatch_started
    outcome = "failed"
    if claimed_action and started and error.request_started is not False:
        outcome = "result_unknown"
    elif (
        error.category == "recovery"
        and error.code == "scope_has_unresolved_action"
    ):
        outcome = "result_unknown"
    elif error.code in {"budget_exceeded", "rate_limit_exceeded"}:
        outcome = "budget_exceeded"
    elif error.code in {
        "tasks_unsupported",
        "unsupported_auth",
        "required_capability_unsupported",
    }:
        outcome = "unsupported"
    elif error.code == "user_stopped":
        outcome = "cancel_requested"
    elif error.code == "confirmation_wait_ended":
        outcome = "deferred"
    elif error.code == "action_rejected":
        outcome = "rejected"
    if error.request_started is False:
        started = False
    return ClassifiedResult(
        outcome,
        error_category=error.category,
        native_error={"code": error.code},
        dispatch_started=started,
    )
