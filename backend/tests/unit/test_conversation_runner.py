"""会話応答の推論Runner port（`app.inference.conversation_runner`）のunit試験。

portの委譲実装が既存InferenceRouterと同じ結果・例外を返すことを、
同一Routerへの直接呼出との比較で検証する。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from unittest.mock import MagicMock

import pytest

from app.inference.authorization import InferenceCaller
from app.inference.config import resolve_inference_settings
from app.inference.contracts import (
    EmbeddingRequest,
    EmbeddingResult,
    InferenceCapability,
    InferenceMessage,
    InferenceTarget,
    InferenceUsage,
    ModelProbeResult,
    ProviderTextResult,
    StructuredGenerationRequest,
    TextGenerationRequest,
    TextGenerationResult,
    TokenEstimate,
    TokenEstimateAccuracy,
    TokenEstimateRequest,
)
from app.inference.conversation_runner import (
    ConversationInferenceRunner,
    create_conversation_inference_runner,
)
from app.inference.errors import InferenceError, InferenceErrorCategory
from app.inference.registry import default_provider_registry
from app.inference.router import InferenceRouter


def _environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for token, model in {
        "CHAT": "gemma4:e4b",
        "PRIVACY": "gemma4:e4b",
        "MEMORY_EXTRACTION": "gemma4:e4b",
        "MEMORY_CONSOLIDATION": "gemma4:12b",
        "EMBEDDING": "nomic-embed-text:latest",
    }.items():
        environment[f"INFERENCE_TARGET_{token}"] = f"ollama/{model}"
        environment[f"INFERENCE_TARGET_{token}_MAX_INPUT_TOKENS"] = "8192"
        if token != "EMBEDDING":
            environment[f"INFERENCE_TARGET_{token}_MAX_OUTPUT_TOKENS"] = "1024"
    return environment


def _messages() -> tuple[InferenceMessage, ...]:
    return (InferenceMessage("user", "hello"),)


class _FakeAdapter:
    provider_id = "ollama"
    capabilities = frozenset(InferenceCapability)

    def __init__(self) -> None:
        self.text_calls = 0
        self.estimate_calls = 0
        self.stream_calls: list[TextGenerationRequest] = []
        self.text_result = ProviderTextResult(
            "ok",
            InferenceUsage(3, 2, 5, provider_reported=True),
        )
        self.stream_deltas: tuple[str, ...] = ("d1", "d2")
        self.estimate_result = TokenEstimate(
            12, TokenEstimateAccuracy.EXACT, "fixture"
        )
        self.failure: InferenceError | None = None
        self.last_estimate_request: TokenEstimateRequest | None = None
        self.last_text_request: TextGenerationRequest | None = None

    def probe(
        self, model_id: str, *, timeout_seconds: float
    ) -> ModelProbeResult:
        del model_id, timeout_seconds
        return ModelProbeResult()

    def generate_text(self, request: TextGenerationRequest) -> ProviderTextResult:
        self.text_calls += 1
        self.last_text_request = request
        if self.failure is not None:
            raise self.failure
        return self.text_result

    async def stream_text(
        self, request: TextGenerationRequest
    ) -> AsyncIterator[str]:
        self.stream_calls.append(request)
        for delta in self.stream_deltas:
            yield delta

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> ProviderTextResult:
        raise NotImplementedError

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        raise NotImplementedError

    def estimate_input_tokens(
        self, request: TokenEstimateRequest
    ) -> TokenEstimate:
        self.estimate_calls += 1
        self.last_estimate_request = request
        return self.estimate_result


def _router(adapter: _FakeAdapter) -> InferenceRouter:
    registry = default_provider_registry()
    registry.bind(adapter)
    settings = resolve_inference_settings(_environment(), registry)
    return InferenceRouter(settings=settings, registry=registry)


def _runner(router: InferenceRouter) -> ConversationInferenceRunner:
    return create_conversation_inference_runner(router)


class TestConversationInferenceRunnerPort:
    def test_generate_text_matches_direct_router_result(self) -> None:
        adapter = _FakeAdapter()
        router = _router(adapter)
        runner = _runner(router)

        direct = router.generate_text(
            caller=InferenceCaller.CHAT,
            target=InferenceTarget.CHAT,
            messages=_messages(),
        )
        via_port = runner.generate_text(
            target=InferenceTarget.CHAT,
            messages=_messages(),
        )

        assert via_port == direct == TextGenerationResult(
            text="ok",
            usage=InferenceUsage(3, 2, 5, provider_reported=True),
        )
        assert adapter.last_text_request is not None
        assert adapter.last_text_request.messages == _messages()

    def test_stream_text_yields_the_same_deltas_as_direct_router(self) -> None:
        adapter = _FakeAdapter()
        router = _router(adapter)
        runner = _runner(router)

        async def collect(
            source: Callable[[], AsyncIterator[str]],
        ) -> list[str]:
            return [delta async for delta in source()]

        async def exercise() -> tuple[list[str], list[str]]:
            direct = await collect(
                lambda: router.stream_text(
                    caller=InferenceCaller.CHAT,
                    target=InferenceTarget.CHAT,
                    messages=_messages(),
                    latency_sensitive=True,
                )
            )
            via_port = await collect(
                lambda: runner.stream_text(
                    target=InferenceTarget.CHAT,
                    messages=_messages(),
                    latency_sensitive=True,
                )
            )
            return direct, via_port

        direct, via_port = asyncio.run(exercise())

        assert via_port == direct == ["d1", "d2"]
        assert adapter.stream_calls[0].latency_sensitive is True
        assert adapter.stream_calls[1].latency_sensitive is True

    def test_estimate_input_tokens_matches_direct_router_result(self) -> None:
        adapter = _FakeAdapter()
        router = _router(adapter)
        runner = _runner(router)

        direct = router.estimate_input_tokens(
            caller=InferenceCaller.CHAT,
            target=InferenceTarget.CHAT,
            messages=_messages(),
        )
        via_port = runner.estimate_input_tokens(
            target=InferenceTarget.CHAT,
            messages=_messages(),
        )

        assert via_port == direct == TokenEstimate(
            12, TokenEstimateAccuracy.EXACT, "fixture"
        )
        assert adapter.last_estimate_request is not None
        assert adapter.last_estimate_request.messages == _messages()

    def test_provider_error_propagates_with_same_category(self) -> None:
        adapter = _FakeAdapter()
        adapter.failure = InferenceError(
            InferenceErrorCategory.UNAVAILABLE,
            retryable=True,
        )
        router = _router(adapter)
        runner = _runner(router)

        with pytest.raises(InferenceError) as direct_error:
            router.generate_text(
                caller=InferenceCaller.CHAT,
                target=InferenceTarget.CHAT,
                messages=_messages(),
            )
        adapter.text_calls = 0
        with pytest.raises(InferenceError) as port_error:
            runner.generate_text(
                target=InferenceTarget.CHAT,
                messages=_messages(),
            )

        assert port_error.value.category is direct_error.value.category
        assert port_error.value.retryable is direct_error.value.retryable

    def test_input_limit_excess_rejects_the_same_way(self) -> None:
        adapter = _FakeAdapter()
        adapter.estimate_result = TokenEstimate(
            8193, TokenEstimateAccuracy.EXACT, "fixture"
        )
        router = _router(adapter)
        runner = _runner(router)

        with pytest.raises(InferenceError) as direct_error:
            router.estimate_input_tokens(
                caller=InferenceCaller.CHAT,
                target=InferenceTarget.CHAT,
                messages=_messages(),
            )
        with pytest.raises(InferenceError) as port_error:
            runner.estimate_input_tokens(
                target=InferenceTarget.CHAT,
                messages=_messages(),
            )

        assert direct_error.value.category is InferenceErrorCategory.INVALID_REQUEST
        assert port_error.value.category is direct_error.value.category
        assert port_error.value.retryable is direct_error.value.retryable

    def test_stream_cancellation_matches_direct_router(self) -> None:
        class _BlockingStreamAdapter(_FakeAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()

            async def stream_text(
                self, request: TextGenerationRequest
            ) -> AsyncIterator[str]:
                self.entered.set()
                # Provider応答を待つ間はcancelのみで抜ける。
                await asyncio.Event().wait()
                yield "late"

        adapter = _BlockingStreamAdapter()
        router = _router(adapter)
        runner = _runner(router)

        async def cancel_and_collect(
            source: Callable[[], AsyncIterator[str]],
        ) -> str:
            async def consume() -> None:
                async for _ in source():
                    pass

            task = asyncio.create_task(consume())
            await adapter.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return "cancelled"

        async def exercise() -> tuple[str, str]:
            direct = await cancel_and_collect(
                lambda: router.stream_text(
                    caller=InferenceCaller.CHAT,
                    target=InferenceTarget.CHAT,
                    messages=_messages(),
                )
            )
            adapter.entered.clear()
            via_port = await cancel_and_collect(
                lambda: runner.stream_text(
                    target=InferenceTarget.CHAT,
                    messages=_messages(),
                )
            )
            return direct, via_port

        direct, via_port = asyncio.run(exercise())

        assert direct == via_port == "cancelled"


class TestConversationRunnerChatCallers:
    """portが会話応答のCallerを既存のCHAT呼出へ固定することを確認する。"""

    def test_generate_text_uses_chat_caller(self) -> None:
        router = MagicMock(spec=InferenceRouter)
        router.generate_text.return_value = TextGenerationResult("ok", None)
        runner = _runner(router)

        runner.generate_text(
            target=InferenceTarget.CHAT,
            messages=_messages(),
        )

        assert router.generate_text.call_args.kwargs["caller"] is InferenceCaller.CHAT

    def test_stream_text_uses_chat_caller(self) -> None:
        router = MagicMock(spec=InferenceRouter)

        stream_calls: list[dict] = []

        async def stream(**kwargs: object) -> AsyncIterator[str]:
            stream_calls.append(kwargs)
            yield "ok"

        router.stream_text = stream
        runner = _runner(router)

        async def exercise() -> list[str]:
            return [
                delta
                async for delta in runner.stream_text(
                    target=InferenceTarget.CHAT,
                    messages=_messages(),
                )
            ]

        assert asyncio.run(exercise()) == ["ok"]
        assert len(stream_calls) == 1
        assert stream_calls[0]["caller"] is InferenceCaller.CHAT

    def test_estimate_input_tokens_uses_chat_caller(self) -> None:
        router = MagicMock(spec=InferenceRouter)
        router.estimate_input_tokens.return_value = TokenEstimate(
            1, TokenEstimateAccuracy.EXACT, "fixture"
        )
        runner = _runner(router)

        runner.estimate_input_tokens(
            target=InferenceTarget.CHAT,
            messages=_messages(),
        )

        call = router.estimate_input_tokens.call_args
        assert call.kwargs["caller"] is InferenceCaller.CHAT
        assert call.kwargs["target"] is InferenceTarget.CHAT
