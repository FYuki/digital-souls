"""A deliberately bounded HTTP API. Only the outbound SDK owns provider transport."""

import asyncio
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine
from contextlib import aclosing
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from .application import CoreError, Inference
from .character import load_characters
from .contracts import AliasCompletion, CharacterCompletion, CompletionInput
from .conversation_api import register_conversations
from .conversations import Conversations
from .history import HistoryPolicy, HistoryStore, Receipt, TurnInput
from .local_http import LocalHTTPBoundary
from .provider import LiteLLMProvider


def error_body(error: CoreError) -> dict[str, dict[str, str]]:
    return {"error": {"type": "core_error", "code": error.code, "message": error.message}}


class ManagedStream(StreamingResponse):
    """Own prefetched upstream even if the client leaves before the first send."""

    upstream: AsyncGenerator[dict[str, Any]]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


async def finish_owned(future: asyncio.Future[Any]) -> bool:
    """Join owned cleanup despite direct Task.cancel; report cancellation to re-raise."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    future.result()
    return cancelled


async def until_disconnect[T](operation: Coroutine[Any, Any, T], request: Request) -> T:
    """Keep result ownership until child cleanup finishes and return cannot suspend."""

    async def disconnected() -> None:
        # FastAPI has already consumed the JSON body before invoking the route.
        while True:
            event = await request.receive()
            if event["type"] == "http.disconnect":
                # Cancel here: parent notification alone races with history commit.
                work.cancel()
                return

    work = asyncio.create_task(operation)
    watcher = asyncio.create_task(disconnected())
    result_ready = False
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            raise CoreError(499, "client_disconnected", "Client disconnected")
        result = work.result()
        result_ready = True
    finally:
        for task in (work, watcher):
            if not task.done() and not task.cancelling():
                task.cancel()
        # AnyIO scopes and direct asyncio Task.cancel require separate shielding.
        # Always join cleanup; do not detach children or cancel their finalizers twice.
        with anyio.CancelScope(shield=True):
            cancelled = await finish_owned(asyncio.gather(work, watcher, return_exceptions=True))
            if (
                (not result_ready or cancelled)
                and not work.cancelled()
                and work.exception() is None
            ):
                abandoned = work.result()
                if isinstance(abandoned, ManagedStream):
                    cancelled = (
                        await finish_owned(asyncio.ensure_future(abandoned.upstream.aclose()))
                        or cancelled
                    )
            if cancelled:
                raise asyncio.CancelledError
    # No await after cleanup: a candidate response becomes caller-owned only here.
    return result


def create_app(
    inference: Inference | None = None,
    *,
    history_store: HistoryStore | None = None,
    history_policy: HistoryPolicy | None = None,
) -> FastAPI:
    """Load the operator registry once or inject a service for offline integration.

    No characters are registered implicitly. Host/Origin checks supplement the
    loopback-only, single-user deployment contract; they do not provide identity.
    """
    if inference is None:
        config_path = os.environ.get("CORE_CHARACTER_CONFIG")
        characters = load_characters(Path(config_path)) if config_path else ()
        inference = Inference(characters, LiteLLMProvider())
    service = inference
    app = FastAPI(title="Digital Souls Core", version="0.1.0")
    app.add_middleware(LocalHTTPBoundary)

    @app.exception_handler(CoreError)
    async def core_error_handler(request: Request, error: CoreError) -> JSONResponse:
        return JSONResponse(error_body(error), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # System rejection also takes precedence when another input field is invalid.
        body = error.body
        if request.url.path.endswith("/completions") and isinstance(body, dict):
            messages = body.get("messages")
            if isinstance(messages, list) and any(
                isinstance(message, dict) and message.get("role") == "system"
                for message in messages
            ):
                return JSONResponse(
                    error_body(
                        CoreError(
                            400,
                            "system_message_unsupported",
                            "Caller system messages are unsupported",
                        )
                    ),
                    status_code=400,
                )
        # Never echo caller input or Pydantic's input-bearing validation details.
        return JSONResponse(
            error_body(CoreError(400, "invalid_request", "Invalid or unsupported input")),
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            error_body(CoreError(500, "internal_error", "Inference failed")), status_code=500
        )

    async def run(
        selector: str, body: CompletionInput, *, alias: bool
    ) -> JSONResponse | StreamingResponse:
        try:
            prepared = await service.prepare(selector, body, alias=alias)
        except CoreError:
            raise
        except Exception:
            raise CoreError(500, "context_error", "Context preparation failed") from None
        profile = prepared.character.config.profile
        service.check(prepared)
        if not body.stream:
            result = await service.provider.complete(profile, prepared.payload)
            return JSONResponse(result, headers=prepared.headers)
        upstream = service.provider.stream(profile, prepared.payload)
        # Prefetch so initial upstream errors use an HTTP error status, before SSE headers.
        try:
            first = await anext(upstream)
        except StopAsyncIteration:
            await upstream.aclose()
            raise CoreError(502, "empty_stream", "Provider returned an empty stream") from None

        async def events() -> AsyncIterator[str]:
            async with aclosing(upstream):
                try:
                    yield "data: " + json.dumps(first, ensure_ascii=False) + "\n\n"
                    async for chunk in upstream:
                        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as error:
                    public_error = (
                        error
                        if isinstance(error, CoreError)
                        else CoreError(502, "provider_error", "Provider stream failed")
                    )
                    yield "event: error\ndata: " + json.dumps(error_body(public_error)) + "\n\n"

        response = ManagedStream(
            events(),
            media_type="text/event-stream",
            headers={**prepared.headers, "Cache-Control": "no-cache"},
        )
        response.upstream = upstream
        return response

    @app.post("/v1/character/completions", response_model=None)
    async def character_completion(
        body: CharacterCompletion, request: Request
    ) -> JSONResponse | StreamingResponse:
        """Infer as a registered character; credentials and endpoint stay server-owned."""
        return await until_disconnect(run(body.character_id, body, alias=False), request)

    @app.post("/v1/chat/completions", response_model=None)
    async def alias_completion(
        body: AliasCompletion, request: Request
    ) -> JSONResponse | StreamingResponse:
        """Resolve a character alias through the same inference path as the explicit API."""
        return await until_disconnect(run(body.model, body, alias=True), request)

    @app.get("/v1/models")
    async def models() -> dict[str, object]:
        """List character aliases, not physical model capabilities or credentials."""
        return {
            "object": "list",
            "data": [
                {
                    "id": char.config.alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": "digital-souls-core",
                }
                for char in service.characters.values()
            ],
        }

    @app.get("/v1/characters/{character_id}")
    async def character_metadata(character_id: str) -> dict[str, object]:
        """Expose pinned identity/profile metadata with unknown physical limits as null."""
        character = service.characters.get(character_id)
        if character is None:
            raise CoreError(404, "character_not_found", "Unknown character")
        config = character.config
        return {
            "character_id": config.character_id,
            "config_version": config.config_version,
            "card_sha256": character.card_sha256,
            "model_alias": config.alias,
            "inference_profile": config.profile.profile_id,
            "provider_model": config.profile.model,
            "allowed_parameters": sorted(config.profile.allowed_parameters),
            "external_send_allowed": config.profile.external_send_allowed,
            "prompt_budget": config.prompt_budget.model_dump(),
            "memory_floor": config.memory_floor,
            "history_floor": config.history_floor,
            "model_context_window_tokens": None,
            "capability_verification": "operator_configured_not_live_verified",
        }

    if history_store is not None:
        conversations = Conversations(service, history_store, history_policy)

        async def run_conversation(
            character_id: str, conversation_id: str, body: TurnInput, request: Request
        ) -> Receipt:
            return await until_disconnect(
                conversations.complete(character_id, conversation_id, body), request
            )

        register_conversations(app, conversations, run_conversation)

    return app
