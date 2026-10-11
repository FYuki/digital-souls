"""Request-owned official SDK client shared by verified local adapters."""

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
import httpx
import openai

from .character import managed_loopback_endpoint


@asynccontextmanager
async def local_openai_client(
    endpoint: str, timeout_seconds: float
) -> AsyncGenerator[openai.AsyncOpenAI]:
    base = managed_loopback_endpoint(endpoint)
    # The pinned SDK unconditionally merges this environment variable into
    # default_headers, even with explicit defaults/copy options. Fail closed
    # before constructing a client instead of modifying global environment or
    # relying on private SDK attributes to remove arbitrary credentials/Host.
    if os.environ.get("OPENAI_CUSTOM_HEADERS"):
        raise ValueError("Local SDK client does not accept environment custom headers")
    async with local_http_client(timeout_seconds) as http:
        client = openai.AsyncOpenAI(
            base_url=base,
            api_key="local-no-auth",
            admin_api_key="",
            webhook_secret="",
            organization="",
            project="",
            http_client=http,
            max_retries=0,
            timeout=timeout_seconds,
        )
        # Empty explicit values suppress SDK environment fallback; omit headers.
        client.organization = None
        client.project = None
        yield client


@asynccontextmanager
async def local_http_client(timeout_seconds: float) -> AsyncGenerator[httpx.AsyncClient]:
    """No proxy/redirect, owned by one operation; cleanup completes on cancellation."""
    http = httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout_seconds)
    try:
        yield http
    finally:
        with anyio.CancelScope(shield=True):
            closing = asyncio.create_task(http.aclose())
            cancelled = False
            while True:
                try:
                    await asyncio.shield(closing)
                    break
                except asyncio.CancelledError:
                    if closing.cancelled():
                        raise
                    # Raw Task.cancel(), including another nested deadline, is
                    # not blocked by AnyIO shields. Finish owned cleanup first.
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
