from __future__ import annotations

import importlib

import pytest

from app.external_mcp.models import MCPFailure


def _classification_module():
    try:
        return importlib.import_module("app.external_mcp.result_classification")
    except ModuleNotFoundError as error:
        if error.name == "app.external_mcp.result_classification":
            pytest.fail("Execution Gate result classification boundary is not implemented")
        raise


def _value(result: object, name: str) -> object:
    if isinstance(result, dict):
        return result.get(name)
    return getattr(result, name)


@pytest.mark.parametrize(
    ("payload", "outcome", "error_category"),
    [
        ({"content": [{"type": "text", "text": "ok"}]}, "succeeded", None),
        (
            {"isError": True, "content": [{"type": "text", "text": "failed"}]},
            "failed",
            "tool_error",
        ),
    ],
)
def test_classification_preserves_success_and_tool_error_outcomes(
    payload: dict[str, object], outcome: str, error_category: str | None
):
    module = _classification_module()
    classified = module.classify_native_payload(payload)

    assert _value(classified, "outcome") == outcome
    assert _value(classified, "error_category") == error_category


def test_classification_accepts_only_formal_elicitation_requests():
    module = _classification_module()
    classified = module.classify_native_payload(
        {
            "resultType": "input_required",
            "requestState": "opaque",
            "inputRequests": {
                "answer": {
                    "method": "elicitation/create",
                    "params": {
                        "mode": "form",
                        "requestedSchema": {"type": "object"},
                    },
                }
            },
        }
    )

    assert _value(classified, "outcome") == "input_required"
    assert _value(classified, "request_ids") == frozenset({"answer"})


@pytest.mark.parametrize(
    ("payload_kind", "claimed_action", "dispatch_started", "outcome"),
    [
        ("invalid", False, False, "failed"),
        ("invalid", False, True, "failed"),
        ("invalid", True, False, "failed"),
        ("invalid", True, True, "result_unknown"),
        ("unsupported", False, False, "unsupported"),
        ("unsupported", False, True, "unsupported"),
        ("unsupported", True, False, "unsupported"),
        ("unsupported", True, True, "result_unknown"),
    ],
)
def test_classification_preserves_reason_and_pending_boundary(
    payload_kind: str,
    claimed_action: bool,
    dispatch_started: bool,
    outcome: str,
):
    module = _classification_module()
    payload = (
        {
            "resultType": "input_required",
            "requestState": {},
            "inputRequests": {},
        }
        if payload_kind == "invalid"
        else {
            "resultType": "input_required",
            "inputRequests": {
                "answer": {
                    "method": "sampling/createMessage",
                    "params": {"note": "elicitation/create"},
                }
            },
        }
    )
    classified = module.classify_native_payload(
        payload,
        claimed_action=claimed_action,
        dispatch_started=dispatch_started,
    )

    expected_category = "protocol" if payload_kind == "invalid" else "policy"
    expected_error = (
        "invalid_input_required"
        if payload_kind == "invalid"
        else "required_capability_unsupported"
    )
    assert _value(classified, "outcome") == outcome
    assert _value(classified, "error_category") == expected_category
    assert _value(classified, "native_error") == {"code": expected_error}
    assert _value(classified, "dispatch_started") is dispatch_started
    assert _value(classified, "request_ids") in (None, frozenset())
    assert _value(classified, "interaction_id") is None


@pytest.mark.parametrize(
    ("error", "claimed", "started", "outcome", "dispatch_started"),
    [
        (
            MCPFailure("transport", "lost"),
            True,
            True,
            "result_unknown",
            True,
        ),
        (
            MCPFailure("transport", "rejected", request_started=False),
            True,
            True,
            "failed",
            False,
        ),
        (
            MCPFailure("policy", "budget_exceeded"),
            False,
            False,
            "budget_exceeded",
            False,
        ),
        (
            MCPFailure("policy", "user_stopped"),
            False,
            False,
            "cancel_requested",
            False,
        ),
    ],
)
def test_classification_projects_one_failure_result(
    error: MCPFailure,
    claimed: bool,
    started: bool,
    outcome: str,
    dispatch_started: bool,
):
    module = _classification_module()
    classified = module.classify_mcp_failure(
        error,
        claimed_action=claimed,
        dispatch_started=started,
    )

    assert _value(classified, "outcome") == outcome
    assert _value(classified, "dispatch_started") is dispatch_started
    assert _value(classified, "error_category") == error.category
    assert _value(classified, "native_error") == {"code": error.code}
