"""LiteLLM adapter: transport only, with no tool execution or routing fallback."""

import inspect
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx
import openai

from .application import CoreError
from .character import Profile
from .local_sdk import local_openai_client

# Keep SDK import-time metadata access offline. No credentials are copied or logged.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import litellm as litellm  # noqa: E402
from litellm.main import responses_api_bridge_check  # noqa: E402

litellm.suppress_debug_info = True
litellm.set_verbose = False
litellm.turn_off_message_logging = True
litellm.telemetry = False


def require_native_chat_route(
    model: str, payload: dict[str, Any], *, local_native_chat: bool = False
) -> None:
    """Use the pinned SDK's local routing probe, denying unknown/Responses modes.

    Route metadata is not proof of model capabilities or authorization to use it.
    """
    code = "unsupported_stream_provider" if payload.get("stream") else "unsupported_provider_route"
    provider, _, name = model.partition("/")
    aliases = litellm.model_alias_map or {}
    if model in aliases or name in aliases:
        raise CoreError(
            400,
            code,
            "SDK model aliases cannot override the verified route",
        )
    if provider != "openai" or name.startswith("responses/"):
        raise CoreError(400, code, "Route requires native OpenAI Chat Completions")
    try:
        info, resolved = responses_api_bridge_check(
            model=name,
            custom_llm_provider="openai",
            tools=payload.get("tools"),
            reasoning_effort=payload.get("reasoning_effort"),
            web_search_options=payload.get("web_search_options"),
            reasoning_summary=payload.get("reasoning_summary"),
            api_base=payload.get("api_base"),
        )
    except Exception:
        raise CoreError(400, code, "Chat route is not verified") from None
    verified_modes = {"chat", None} if local_native_chat else {"chat"}
    if info.get("mode") not in verified_modes or resolved != name:
        raise CoreError(400, code, "Route is not native Chat Completions")


def provider_error(error: Exception) -> CoreError:
    """Classify bounded exception chains without returning provider text or secrets."""
    # Streaming can expose HTTPX directly or wrap the cause in MidStreamFallbackError.
    # Classify types only: never inspect/return messages, URLs, headers or generated text.
    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending and len(visited) < 8:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, litellm.UnsupportedParamsError):
            return CoreError(
                400, "unsupported_parameter", "Provider rejected an unsupported parameter"
            )
        if isinstance(
            current, (litellm.Timeout, httpx.TimeoutException, openai.APITimeoutError, TimeoutError)
        ):
            return CoreError(504, "provider_timeout", "Provider timed out")
        if isinstance(current, (litellm.RateLimitError, openai.RateLimitError)):
            return CoreError(429, "provider_rate_limit", "Provider rate limit reached")
        for cause in (
            getattr(current, "original_exception", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(cause, BaseException):
                pending.append(cause)
    return CoreError(502, "provider_error", "Provider request failed")


def public_completion(result: dict[str, Any]) -> dict[str, Any]:
    """Expose only public message/delta fields, never SDK reasoning extensions."""
    for choice in result.get("choices", []):
        for key in ("message", "delta"):
            if key not in choice:
                continue
            message = choice[key]
            choice[key] = {
                field: value
                for field, value in message.items()
                if field in {"role", "content", "tool_calls", "tool_call_id"}
            }
            # A reasoning-only terminal message is still a replayable empty text.
            if key == "message" and not choice[key].get("tool_calls"):
                choice[key]["content"] = choice[key].get("content") or ""
    return result


class LiteLLMProvider:
    @staticmethod
    @asynccontextmanager
    async def _client(profile: Profile) -> AsyncGenerator[openai.AsyncOpenAI | None]:
        if profile.transport != "llamacpp_chat":
            yield None
            return
        assert profile.api_base is not None
        async with local_openai_client(profile.api_base, profile.timeout_seconds) as client:
            yield client

    @staticmethod
    async def _call(
        profile: Profile, payload: dict[str, Any], client: openai.AsyncOpenAI | None = None
    ) -> Any:
        if not profile.external_send_allowed:
            raise CoreError(403, "external_send_denied", "Character export policy denies inference")
        provider_options: dict[str, Any] = {}
        if profile.transport == "llamacpp_chat":
            # LiteLLM can override the injected SDK client's organization from
            # its own global or alternate environment name. Do not forward it.
            if litellm.organization or os.environ.get("OPENAI_ORGANIZATION"):
                raise CoreError(403, "local_configuration_denied", "Local SDK configuration denied")
            # A public dummy value satisfies the SDK without forwarding cloud credentials.
            provider_options.update(api_base=profile.api_base, api_key="local-no-auth")
        if client is not None:
            provider_options["client"] = client
        if payload.get("stream") or profile.transport == "llamacpp_chat":
            # Pinned LiteLLM adapters do not share a response ownership contract.
            # Only the native OpenAI SDK stream lifecycle is verified here.
            require_native_chat_route(
                profile.model,
                {**payload, **provider_options},
                local_native_chat=profile.transport == "llamacpp_chat",
            )
        if profile.model.startswith("ollama/"):
            raise CoreError(
                400, "unsupported_provider_route", "Ollama requires the native ollama_chat route"
            )
        if profile.model.startswith("ollama_chat/"):
            # The pinned SDK still silently removes tool_choice. Do not claim support.
            if "tool_choice" in payload:
                raise CoreError(400, "unsupported_parameter", "Ollama tool_choice is not supported")
            if "think" in payload or "reasoning_effort" in payload:
                raise CoreError(
                    400, "unsupported_parameter", "Thinking is controlled by the operator profile"
                )
            if profile.ollama_think is not None:
                provider_options["think"] = profile.ollama_think
        return await litellm.acompletion(
            model=profile.model,
            **payload,
            **provider_options,
            timeout=profile.timeout_seconds,
            num_retries=0,
            max_retries=0,
            drop_params=False,
            fallbacks=[],
        )

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        """Make one SDK call using the fixed profile, preserving normalized completion fields."""
        try:
            async with self._client(profile) as client:
                result = await self._call(profile, payload, client)
            return public_completion(dict(result.model_dump(exclude_none=True)))
        except CoreError:
            raise
        except Exception as error:
            raise provider_error(error) from None

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        async with self._client(profile) as client:
            iterator = self._stream(profile, payload, client)
            try:
                async for chunk in iterator:
                    yield chunk
            finally:
                with anyio.CancelScope(shield=True):
                    await iterator.aclose()

    async def _stream(
        self, profile: Profile, payload: dict[str, Any], client: openai.AsyncOpenAI | None
    ) -> AsyncGenerator[dict[str, Any]]:
        """Yield SDK deltas and own transport cleanup on EOF, errors, and cancellation.

        Only verified native Chat Completions routes may acquire a stream.
        """
        upstream = None
        try:
            upstream = await self._call(profile, payload, client)
            async for chunk in upstream:
                yield public_completion(chunk.model_dump(exclude_none=True))
        except CoreError:
            raise
        except Exception as error:
            raise provider_error(error) from None
        finally:
            if upstream is not None:
                # The pinned LiteLLM wrapper exposes its underlying SDK stream.
                # OpenAI AsyncStream uses close(); async generators use aclose().
                transport = getattr(upstream, "completion_stream", upstream)
                close = getattr(transport, "aclose", None) or getattr(transport, "close", None)
                if close is not None:
                    # Starlette disconnect uses an AnyIO cancelled scope. Cleanup
                    # must survive its repeated cancellation at I/O checkpoints.
                    with anyio.CancelScope(shield=True):
                        try:
                            result = close()
                            if inspect.isawaitable(result):
                                await result
                        except Exception:
                            raise CoreError(
                                502, "provider_error", "Provider stream close failed"
                            ) from None
