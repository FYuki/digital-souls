"""外部結果の不正形状、会話binding、入力予算のレビュー回帰。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.external_mcp import ExecutionContext
from app.external_mcp.models import MCPFailure
from app.inference import (
    InferenceCancellationToken,
    InferenceError,
    InferenceErrorCategory,
)
from app.tool_use.binding import BindingTarget
from app.tool_use.projection import Sanitizer
from app.tool_use.routing import InferenceDecisionRouter
from app.tool_use.service import ToolService
from tests.tool_use_test_support import Decisions, Scanner, runtime
from tests.external_mcp_test_support import manifest


@pytest.mark.parametrize(
    "native",
    [
        "bad",
        {"content": "bad"},
        {"content": {}},
        {"content": [None, 1, "bad", {"type": "resource", "resource": "bad"}]},
    ],
)
def test_malformed_result_does_not_crash(native):
    result = Sanitizer(Scanner()).result(
        {"outcome": "succeeded", "native_payload": native}
    )
    assert result["text"] == []
    assert result["omitted"]


@pytest.mark.parametrize(
    "structured", [list(range(65)), {str(n): n for n in range(129)}, "x" * 16_385]
)
def test_structured_truncation_is_explicit(structured):
    result = Sanitizer(Scanner()).result(
        {"outcome": "succeeded", "native_payload": {"structuredContent": structured}}
    )
    assert result["omitted"] is True


@pytest.mark.parametrize("kind", ["list", "dict", "string", "depth", "complete"])
def test_recovered_projection_reports_internal_omission(kind):
    values = {
        "list": list(range(65)), "dict": {str(n): n for n in range(129)},
        "string": "x" * 16_385, "complete": {"count": 1},
    }
    deep = 1
    for _ in range(14):
        deep = {"child": deep}
    values["depth"] = deep
    result = Sanitizer(Scanner()).result({
        "outcome": "succeeded", "replayed": True,
        "result_projection": {"structured": values[kind]},
    })
    assert result.get("omitted", False) is (kind != "complete")
    assert result["outcome"] == "succeeded" and result["replayed"] is True
    if kind == "complete":
        assert result["structured"] == values[kind]


def test_binding_from_other_conversation_is_denied_and_close_releases_it():
    async def run():
        target = BindingTarget(
            "target", "external-a", "miori", "対象", ("native-tool",)
        )
        config = manifest(connection_id="external-a", binding=True)
        async with runtime(Decisions(), config=config, targets=(target,)) as (
            service,
            source,
            gate,
        ):
            binding, _ = service.bindings.resolve(
                "miori", "one", "external-a", "native-tool"
            )
            loop = gate.begin_loop(ExecutionContext("miori", "two"))
            result = await gate.invoke(
                "external-a", "native-tool", {"value": 1}, loop, binding_id=binding
            )
            assert result["error_category"] == "policy"
            assert not source.calls
            gate.end_loop(loop)
            service._status[("miori", "one")] = {"state": "idle"}
            service.close()
            assert not service.bindings._resolved and not service.bindings._selected

    asyncio.run(run())


@pytest.mark.parametrize(
    "schema",
    [
        {"description": "x" * 16_384},
        {"$ref": "#/local"},
        {"properties": {"value": {"$dynamicRef": "https://example.test/schema"}}},
        {"properties": {"value": {"pattern": "(a+)+$"}}},
        {"properties": {str(n): {"pattern": "abc"} for n in range(17)}},
    ],
)
def test_unsafe_mrtr_schema_is_rejected_before_validator(schema):
    with pytest.raises(MCPFailure, match="unsupported_input_schema"):
        ToolService._validate_interaction_schema(schema)


def test_path_guard_does_not_depend_on_property_name_or_core_cwd(tmp_path):
    async def run():
        async with runtime(Decisions()) as (service, _gate, _source):
            service.protected_roots = (tmp_path / "core",)
            for value in [
                str(tmp_path / "core" / "app.py"),
                "../core/app.py",
                "folder/item.txt",
            ]:
                with pytest.raises(MCPFailure):
                    service._protect_core({"target": value})
            service._protect_core({"target": str(tmp_path / "external" / "item.txt")})

    asyncio.run(run())


def test_completion_input_limit_falls_back_without_losing_original_results():
    class Router:
        def estimate_input_tokens(self, **kwargs):
            if "done" in kwargs["response_schema"]["properties"]:
                raise InferenceError(
                    InferenceErrorCategory.INVALID_REQUEST, retryable=False
                )

        def generate_structured(self, **kwargs):
            return SimpleNamespace(value={"action": "finish"})

    context = {
        "original_request": "結果を教えて",
        "current_user": "結果を教えて",
        "results": [{"outcome": "succeeded", "text": "取得済み"}],
        "candidates": [],
        "history": [],
        "pending": None,
    }
    result = asyncio.run(
        InferenceDecisionRouter(Router()).decide(context, InferenceCancellationToken())
    )
    assert result.action == "finish"
    assert context["results"][0]["text"] == "取得済み"


@pytest.mark.parametrize(
    "native",
    [
        "bad",
        {"inputRequests": {"a": None}},
        {"inputRequests": {"a": {"params": "bad"}}},
    ],
)
def test_malformed_interaction_is_rejected(native):
    async def run():
        async with runtime(Decisions()) as (service, _source, _gate):
            with pytest.raises(MCPFailure):
                service._interaction({"native_payload": native})

    asyncio.run(run())


def test_status_endpoint_never_caches_conversation_data():
    from fastapi import Request, Response
    from uuid import uuid4
    from app.routers.tool_use import status

    response = Response()
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace())})
    assert (
        asyncio.run(status("miori", uuid4(), request, response))["state"] == "disabled"
    )
    assert response.headers["Cache-Control"] == "no-store"
