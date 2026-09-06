"""公式SDK 2.0.0のtransportとnative requestだけを受け持つ。"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncGenerator, AsyncIterator, Awaitable, Callable

import anyio
import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from .models import Connection, Discovery, Json, MCPFailure
from .ports import SecretResolverPort

_private_io: ContextVar[bool] = ContextVar("external_mcp_private_io", default=False)


_http_failures: ContextVar[list[MCPFailure] | None] = ContextVar(
    "external_mcp_http_failures", default=None
)


class _PrivateLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _private_io.get():
            record.msg = "external MCP transport diagnostic suppressed"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


_filter = _PrivateLogFilter()


def _protect_sdk_logs() -> None:
    # SDKはprotocol errorに外部本文を含める。外部I/O task内だけ固定文へ置換する。
    for name in list(logging.Logger.manager.loggerDict):
        if name.split(".")[0] in {"mcp", "httpx2", "httpcore2"}:
            logging.getLogger(name).addFilter(_filter)


class EnvironmentSecrets:
    async def resolve(self, secret_ref: str) -> str:
        value = os.environ.get(secret_ref, "")
        if not value or "\r" in value or "\n" in value:
            raise MCPFailure("auth", "secret_unavailable")
        return value


class _BearerAuth(httpx2.Auth):
    def __init__(self, resolver: SecretResolverPort, ref: str) -> None:
        self.resolver = resolver
        self.ref = ref

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        try:
            token = await self.resolver.resolve(self.ref)
            if not token or "\r" in token or "\n" in token:
                raise ValueError
        except Exception:
            raise MCPFailure("auth", "secret_unavailable") from None
        request.headers["Authorization"] = "Bearer " + token
        yield request


async def _check_http(response: httpx2.Response) -> None:
    failures = _http_failures.get()
    if (
        failures is not None
        and response.status_code >= 500
        and not response.headers.get("content-type", "")
        .lower()
        .startswith("application/json")
    ):
        # SDKがHTTP失敗をJSON-RPC internal errorへ変換する前に、当該requestへ記録する。
        failures.append(MCPFailure("transport", "http_service_error", retryable=True))
    if response.status_code in {401, 403}:
        challenge = response.headers.get("www-authenticate", "").lower()
        code = (
            "unsupported_auth"
            if "resource_metadata" in challenge or "authorization_uri" in challenge
            else "auth_failed"
        )
        failure = MCPFailure("auth", code)
        if failures is None:
            raise failure
        failures.append(failure)
    if response.is_redirect:
        failure = MCPFailure("policy", "relink_required")
        if failures is None:
            raise failure
        failures.append(failure)


def _failure(error: BaseException) -> MCPFailure:
    if isinstance(error, MCPFailure):
        return error
    # ExceptionGroup内の認証失敗も生本文を公開せず分類する。
    nested = getattr(error, "exceptions", ())
    for child in nested:
        failure = _failure(child)
        if failure.category in {"auth", "policy"}:
            return failure
    if isinstance(error, MCPError):
        if error.code == -32003:
            return MCPFailure("policy", "required_capability_unsupported")
        return MCPFailure("protocol", "protocol_error")
    if isinstance(error, httpx2.HTTPStatusError):
        if error.response.status_code >= 500:
            return MCPFailure("transport", "http_service_error", retryable=True)
        return MCPFailure("protocol", "http_request_rejected")
    if isinstance(
        error,
        (
            TimeoutError,
            OSError,
            httpx2.TransportError,
            anyio.EndOfStream,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ),
    ):
        return MCPFailure("transport", "transport_error", retryable=True)
    return MCPFailure("protocol", "invalid_protocol_result")


class ExternalMCPClient:
    def __init__(
        self,
        connection: Connection,
        secrets: SecretResolverPort | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.connection = connection
        self.secrets = secrets or EnvironmentSecrets()
        self.timeout = timeout
        self._client: Client | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[ExternalMCPClient]:
        if self.connected:
            raise MCPFailure("policy", "already_connected")
        _protect_sdk_logs()
        token = _private_io.set(True)
        body_error: BaseException | None = None
        try:
            async with AsyncExitStack() as stack:
                c = self.connection.manifest["connection"]
                if c["transport"] == "stdio":
                    stderr = stack.enter_context(open(os.devnull, "w"))
                    transport = stdio_client(
                        StdioServerParameters(**c["stdio"]), errlog=stderr
                    )
                else:
                    auth = None
                    if c["auth"]["type"] == "bearer":
                        auth = _BearerAuth(self.secrets, c["auth"]["secret_ref"])
                    http = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            auth=auth,
                            follow_redirects=False,
                            trust_env=False,
                            timeout=self.timeout,
                            event_hooks={"response": [_check_http]},
                        )
                    )
                    transport = streamable_http_client(c["endpoint"], http_client=http)
                client = Client(
                    transport,
                    mode="auto",
                    cache=None,
                    read_timeout_seconds=self.timeout,
                )
                # SDK context managerは同じtaskでenter/exitする。
                await stack.enter_async_context(client)
                self._client = client
                try:
                    yield self
                except BaseException as error:
                    body_error = error
                    raise
                finally:
                    self._client = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if body_error is not None:
                raise body_error
            raise _failure(error) from None
        finally:
            self._client = None
            _private_io.reset(token)

    async def _request(self, request: Callable[[Client], Awaitable[Any]]) -> Any:
        if self._client is None:
            raise MCPFailure("unavailable", "not_connected")
        token = _private_io.set(True)
        failures: list[MCPFailure] = []
        failure_token = _http_failures.set(failures)
        try:
            async with asyncio.timeout(self.timeout):
                auth = self.connection.manifest["connection"]["auth"]
                if auth["type"] == "bearer":
                    # SDKのbackground taskを開始する前にも欠落・失効を検出する。
                    try:
                        token_value = await self.secrets.resolve(auth["secret_ref"])
                        if not token_value or "\r" in token_value or "\n" in token_value:
                            raise ValueError
                    except Exception:
                        raise MCPFailure("auth", "secret_unavailable") from None
                result = await request(self._client)
                if failures:
                    raise failures[-1]
                return result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise (failures[-1] if failures else _failure(error)) from None
        finally:
            _private_io.reset(token)
            _http_failures.reset(failure_token)

    async def discover(self) -> Discovery:
        async def fetch(client: Client) -> Discovery:
            lists: dict[str, tuple[Json, ...]] = {}
            for kind in ("tools", "resources", "prompts"):
                if getattr(client.server_capabilities, kind, None) is None:
                    lists[kind] = ()
                    continue
                method = getattr(client, f"list_{kind}")
                values: list[Json] = []
                cursor = None
                cursors: set[str] = set()
                for _ in range(100):
                    page = await method(cursor=cursor, cache_mode="refresh")
                    values.extend(
                        v.model_dump(mode="json", by_alias=True, exclude_none=True)
                        for v in getattr(page, kind)
                    )
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                    if cursor in cursors:
                        raise MCPFailure("protocol", "discovery_cursor_loop")
                    cursors.add(cursor)
                else:
                    raise MCPFailure("protocol", "discovery_page_limit")
                lists[kind] = tuple(values)
            return Discovery(client.protocol_version, **lists)

        result: Discovery = await self._request(fetch)
        return result

    async def call_tool(
        self,
        name: str,
        arguments: Json,
        *,
        input_responses: Json | None = None,
        request_state: str | None = None,
    ) -> Json:
        async def call(client: Client) -> Json:
            result = await client.session.call_tool(
                name,
                arguments,
                allow_input_required=True,
                input_responses=input_responses,
                request_state=request_state,
            )
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)

        result: Json = await self._request(call)
        return result

    async def read_resource(
        self,
        uri: str,
        *,
        input_responses: Json | None = None,
        request_state: str | None = None,
    ) -> Json:
        async def read(client: Client) -> Json:
            result = await client.session.read_resource(
                uri,
                allow_input_required=True,
                input_responses=input_responses,
                request_state=request_state,
            )
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)

        result: Json = await self._request(read)
        return result
