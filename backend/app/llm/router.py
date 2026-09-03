from collections.abc import AsyncIterator
import threading

from app.inference import (
    InferenceMessage,
    InferencePrincipal,
    InferencePrincipalKind,
    InferenceRouter,
    InferenceTarget,
)
from app.llm.base import LLMClient
from app.model_settings import ModelSettings
from app.prompting import BuiltPrompt, PromptMessage

DEFAULT_PROVIDER = "ollama"
_router_lock = threading.Lock()
_configured_routers: list[InferenceRouter] = []
_CHAT_PRINCIPAL = InferencePrincipal(InferencePrincipalKind.CORE, "chat")


def register_inference_router(router: InferenceRouter) -> None:
    with _router_lock:
        _configured_routers.append(router)


def clear_inference_router(router: InferenceRouter) -> None:
    with _router_lock:
        _configured_routers.remove(router)


def current_inference_router() -> InferenceRouter | None:
    with _router_lock:
        return _configured_routers[-1] if _configured_routers else None


def _inference_messages(
    messages: tuple[PromptMessage, ...],
) -> tuple[InferenceMessage, ...]:
    return tuple(
        InferenceMessage(role=message.role.value, content=message.content)
        for message in messages
    )


class _ClaudeClient(LLMClient):
    def generate(
        self,
        prompt: BuiltPrompt,
        *,
        max_output_tokens: int,
    ) -> str:
        raise NotImplementedError("ClaudeClient is not yet implemented")

    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        raise NotImplementedError("ClaudeClient is not yet implemented")


def _create_llm_client(provider: str, settings: ModelSettings) -> LLMClient:
    if provider == "ollama":
        from app.llm.ollama_client import OllamaClient as _OllamaClient

        return _OllamaClient(
            model_name=settings.ollama_chat_model,
            context_tokens=settings.ollama_context_tokens,
        )
    if provider == "claude":
        return _ClaudeClient()
    raise ValueError(f"Unsupported LLM provider: {provider}")


def generate_response(
    prompt: BuiltPrompt,
    *,
    max_output_tokens: int,
    settings: ModelSettings,
) -> str:
    inference_router = current_inference_router()
    if inference_router is not None:
        return inference_router.generate_text(
            principal=_CHAT_PRINCIPAL,
            target=InferenceTarget.CHAT,
            messages=_inference_messages(prompt.messages),
        ).text
    client = _create_llm_client(DEFAULT_PROVIDER, settings)
    return client.generate(prompt, max_output_tokens=max_output_tokens)


def count_input_tokens(
    messages: tuple[PromptMessage, ...], *, settings: ModelSettings
) -> int:
    inference_router = current_inference_router()
    if inference_router is not None:
        return inference_router.estimate_input_tokens(
            principal=_CHAT_PRINCIPAL,
            target=InferenceTarget.CHAT,
            messages=_inference_messages(messages),
        ).count
    client = _create_llm_client(DEFAULT_PROVIDER, settings)
    return client.count_input_tokens(messages)


async def stream_response(
    prompt: BuiltPrompt,
    *,
    max_output_tokens: int,
    settings: ModelSettings,
) -> AsyncIterator[str]:
    inference_router = current_inference_router()
    if inference_router is not None:
        async for delta in inference_router.stream_text(
            principal=_CHAT_PRINCIPAL,
            target=InferenceTarget.CHAT,
            messages=_inference_messages(prompt.messages),
        ):
            yield delta
        return
    client = _create_llm_client(DEFAULT_PROVIDER, settings)
    async for delta in client.stream_generate(
        prompt, max_output_tokens=max_output_tokens
    ):
        yield delta
