from collections.abc import AsyncIterator
import threading

from app.inference import (
    ConversationInferenceRunner,
    InferenceMessage,
    InferenceRouter,
    InferenceTarget,
    create_conversation_inference_runner,
)
from app.model_settings import ModelSettings
from app.prompting import BuiltPrompt, PromptMessage

_router_lock = threading.Lock()
_configured_routers: list[InferenceRouter] = []


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


def _conversation_runner() -> ConversationInferenceRunner:
    inference_router = current_inference_router()
    if inference_router is None:
        raise RuntimeError("inference router is not configured")
    return create_conversation_inference_runner(inference_router)


def generate_response(
    prompt: BuiltPrompt,
    *,
    max_output_tokens: int,
    settings: ModelSettings,
) -> str:
    del max_output_tokens, settings
    runner = _conversation_runner()
    return runner.generate_text(
        target=InferenceTarget.CHAT,
        messages=_inference_messages(prompt.messages),
    ).text


def count_input_tokens(
    messages: tuple[PromptMessage, ...], *, settings: ModelSettings
) -> int:
    del settings
    runner = _conversation_runner()
    return runner.estimate_input_tokens(
        target=InferenceTarget.CHAT,
        messages=_inference_messages(messages),
    ).count


async def stream_response(
    prompt: BuiltPrompt,
    *,
    max_output_tokens: int,
    settings: ModelSettings,
    latency_sensitive: bool = False,
) -> AsyncIterator[str]:
    del max_output_tokens, settings
    runner = _conversation_runner()
    async for delta in runner.stream_text(
        target=InferenceTarget.CHAT,
        messages=_inference_messages(prompt.messages),
        latency_sensitive=latency_sensitive,
    ):
        yield delta
