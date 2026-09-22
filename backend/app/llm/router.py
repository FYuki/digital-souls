from collections.abc import AsyncIterator

from app.inference import (
    InferenceCaller,
    InferenceMessage,
    InferenceRouter,
    InferenceTarget,
)
from app.model_settings import ModelSettings
from app.prompting import BuiltPrompt, PromptMessage
from app.registry import RegistrationStack

_configured_routers = RegistrationStack[InferenceRouter]()
_CHAT_CALLER = InferenceCaller.CHAT


def register_inference_router(router: InferenceRouter) -> None:
    _configured_routers.push(router)


def clear_inference_router(router: InferenceRouter) -> None:
    _configured_routers.remove(router)


def current_inference_router() -> InferenceRouter | None:
    return _configured_routers.current()


def _inference_messages(
    messages: tuple[PromptMessage, ...],
) -> tuple[InferenceMessage, ...]:
    return tuple(
        InferenceMessage(role=message.role.value, content=message.content)
        for message in messages
    )


def generate_response(
    prompt: BuiltPrompt,
    *,
    max_output_tokens: int,
    settings: ModelSettings,
) -> str:
    del max_output_tokens, settings
    inference_router = current_inference_router()
    if inference_router is None:
        raise RuntimeError("inference router is not configured")
    return inference_router.generate_text(
        caller=_CHAT_CALLER,
        target=InferenceTarget.CHAT,
        messages=_inference_messages(prompt.messages),
    ).text


def count_input_tokens(
    messages: tuple[PromptMessage, ...], *, settings: ModelSettings
) -> int:
    inference_router = current_inference_router()
    del settings
    if inference_router is None:
        raise RuntimeError("inference router is not configured")
    return inference_router.estimate_input_tokens(
        caller=_CHAT_CALLER,
        target=InferenceTarget.CHAT,
        messages=_inference_messages(messages),
    ).count


async def stream_response(
    prompt: BuiltPrompt,
    *,
    max_output_tokens: int,
    settings: ModelSettings,
) -> AsyncIterator[str]:
    inference_router = current_inference_router()
    del max_output_tokens, settings
    if inference_router is None:
        raise RuntimeError("inference router is not configured")
    async for delta in inference_router.stream_text(
        caller=_CHAT_CALLER,
        target=InferenceTarget.CHAT,
        messages=_inference_messages(prompt.messages),
    ):
        yield delta
