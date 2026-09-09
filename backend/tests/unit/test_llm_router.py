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
