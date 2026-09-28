from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest


def _built_prompt():
    from tests.prompt_test_support import prompt_build_input, prompt_builder

    return prompt_builder().build(prompt_build_input())


def _settings():
    from app.model_settings import resolve_model_settings

    return resolve_model_settings({})


class TestGenerateResponse:
    def test_should_pass_built_prompt_to_inference_chat_target(self):
        from app.inference import (
            InferenceCaller,
            InferenceRouter,
            InferenceTarget,
            TextGenerationResult,
        )
        from app.llm.router import (
            clear_inference_router,
            generate_response,
            register_inference_router,
        )

        built_prompt = _built_prompt()
        inference_router = MagicMock(spec=InferenceRouter)
        inference_router.generate_text.return_value = TextGenerationResult(
            "光織のLLM応答",
            None,
        )
        register_inference_router(inference_router)
        try:
            result = generate_response(
                built_prompt, max_output_tokens=512, settings=_settings()
            )
        finally:
            clear_inference_router(inference_router)

        assert result == "光織のLLM応答"
        call = inference_router.generate_text.call_args
        assert call.kwargs["caller"] is InferenceCaller.CHAT
        assert call.kwargs["target"] is InferenceTarget.CHAT
        assert [message.content for message in call.kwargs["messages"]] == [
            message.content for message in built_prompt.messages
        ]

    def test_should_fail_without_configured_inference_router(self):
        from app.llm.router import generate_response

        with pytest.raises(RuntimeError, match="inference router is not configured"):
            generate_response(
                _built_prompt(),
                max_output_tokens=512,
                settings=_settings(),
            )


class TestCountInputTokens:
    def test_should_pass_messages_to_inference_chat_target(self):
        from app.inference import (
            InferenceCaller,
            InferenceRouter,
            InferenceTarget,
            TokenEstimate,
            TokenEstimateAccuracy,
        )
        from app.llm.router import (
            clear_inference_router,
            count_input_tokens,
            register_inference_router,
        )

        built_prompt = _built_prompt()
        inference_router = MagicMock(spec=InferenceRouter)
        inference_router.estimate_input_tokens.return_value = TokenEstimate(
            42, TokenEstimateAccuracy.EXACT, "fixture"
        )
        register_inference_router(inference_router)
        try:
            result = count_input_tokens(
                built_prompt.messages, settings=_settings()
            )
        finally:
            clear_inference_router(inference_router)

        assert result == 42
        call = inference_router.estimate_input_tokens.call_args
        assert call.kwargs["caller"] is InferenceCaller.CHAT
        assert call.kwargs["target"] is InferenceTarget.CHAT
        assert [message.content for message in call.kwargs["messages"]] == [
            message.content for message in built_prompt.messages
        ]


class TestRunnerPortWiring:
    """`llm/router.py`の会話応答が選択済みRunner port実装を通ることを確認する。

    利用側は具象Runnerへ依存しないため、port実装の選択箇所である
    `create_conversation_inference_runner`を差し替えて観測する。
    """

    def test_generate_response_calls_runner_port(self):
        from unittest.mock import patch
        from app.inference import (
            InferenceRouter,
            InferenceTarget,
            TextGenerationResult,
        )
        from app.llm.router import (
            clear_inference_router,
            generate_response,
            register_inference_router,
        )
        import app.llm.router as llm_router

        built_prompt = _built_prompt()
        inference_router = MagicMock(spec=InferenceRouter)
        register_inference_router(inference_router)

        selected_runner = MagicMock()
        selected_runner.generate_text.return_value = TextGenerationResult(
            "swapped", None
        )

        with patch.object(
            llm_router,
            "create_conversation_inference_runner",
            return_value=selected_runner,
        ) as factory:
            try:
                result = generate_response(
                    built_prompt, max_output_tokens=512, settings=_settings()
                )
            finally:
                clear_inference_router(inference_router)

        # 選択した実装だけを交換したので、結果は旧Router実装ではなく選択実装のもの。
        assert result == "swapped"
        factory.assert_called_once_with(inference_router)
        inference_router.generate_text.assert_not_called()
        generate_call = selected_runner.generate_text.call_args
        assert generate_call.kwargs["target"] is InferenceTarget.CHAT
        assert [message.content for message in generate_call.kwargs["messages"]] == [
            message.content for message in built_prompt.messages
        ]

    def test_stream_response_calls_runner_port(self):
        import asyncio
        from unittest.mock import patch
        from app.inference import InferenceRouter
        from app.llm.router import (
            clear_inference_router,
            register_inference_router,
            stream_response,
        )
        import app.llm.router as llm_router

        built_prompt = _built_prompt()
        inference_router = MagicMock(spec=InferenceRouter)

        async def direct_stream(**kwargs: object) -> AsyncIterator[str]:
            yield "direct"

        inference_router.stream_text = direct_stream
        register_inference_router(inference_router)

        port_calls: list[dict] = []

        class _SelectedRunner:
            async def stream_text(self, **kwargs: object) -> AsyncIterator[str]:
                port_calls.append(kwargs)
                yield "swapped"

        with patch.object(
            llm_router,
            "create_conversation_inference_runner",
            return_value=_SelectedRunner(),
        ):
            async def exercise() -> list[str]:
                return [
                    chunk
                    async for chunk in stream_response(
                        built_prompt,
                        max_output_tokens=512,
                        settings=_settings(),
                    )
                ]

            try:
                result = asyncio.run(exercise())
            finally:
                clear_inference_router(inference_router)

        assert result == ["swapped"]
        assert len(port_calls) == 1

    def test_count_input_tokens_calls_runner_port(self):
        from unittest.mock import patch
        from app.inference import (
            InferenceRouter,
            TokenEstimate,
            TokenEstimateAccuracy,
        )
        from app.llm.router import (
            clear_inference_router,
            count_input_tokens,
            register_inference_router,
        )
        import app.llm.router as llm_router

        built_prompt = _built_prompt()
        inference_router = MagicMock(spec=InferenceRouter)
        register_inference_router(inference_router)

        selected_runner = MagicMock()
        selected_runner.estimate_input_tokens.return_value = TokenEstimate(
            37, TokenEstimateAccuracy.EXACT, "fixture"
        )

        with patch.object(
            llm_router,
            "create_conversation_inference_runner",
            return_value=selected_runner,
        ):
            try:
                result = count_input_tokens(
                    built_prompt.messages, settings=_settings()
                )
            finally:
                clear_inference_router(inference_router)

        assert result == 37
        inference_router.estimate_input_tokens.assert_not_called()


class TestProviderBoundary:
    def test_router_does_not_expose_infrastructure_clients(self):
        from app.llm import router

        assert not hasattr(router, "__all__")
        assert not hasattr(router, "create_llm_client")
        assert not hasattr(router, "OllamaClient")
        assert not hasattr(router, "ClaudeClient")
        assert not hasattr(router, "_create_llm_client")


@pytest.mark.parametrize('latency_sensitive', [False, True])
def test_streaming_preserves_prompt_and_explicit_latency_intent(latency_sensitive):
    import asyncio
    from app.inference import InferenceRouter, InferenceTarget
    from app.llm.router import register_inference_router, clear_inference_router, stream_response
    calls = []
    router = MagicMock(spec=InferenceRouter)
    async def stream(**kwargs):
        calls.append(kwargs)
        yield 'ok'
    router.stream_text = stream
    prompt = _built_prompt()
    async def exercise():
        return [chunk async for chunk in stream_response(
            prompt, max_output_tokens=512, settings=_settings(), latency_sensitive=latency_sensitive,
        )]
    register_inference_router(router)
    try:
        assert asyncio.run(exercise()) == ['ok']
    finally:
        clear_inference_router(router)
    assert calls[0]['latency_sensitive'] is latency_sensitive
    assert calls[0]['target'] is InferenceTarget.CHAT
    assert [message.content for message in calls[0]['messages']] == [message.content for message in prompt.messages]
