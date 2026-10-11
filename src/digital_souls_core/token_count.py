"""Pinned llama.cpp template + tokenizer adapter, without inference or body logs."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from .character import managed_loopback_endpoint
from .contracts import Message, Tool
from .errors import CoreError
from .local_sdk import local_http_client


class LlamaCppTokenCounter:
    def __init__(
        self,
        endpoint: str,
        timeout_seconds: float,
        *,
        tool_choice: Any = None,
        before_send: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.base = managed_loopback_endpoint(endpoint).removesuffix("/v1")
        self.timeout_seconds = timeout_seconds
        self.tool_choice = tool_choice
        self.before_send = before_send

    @staticmethod
    def region_messages(messages: Sequence[Message]) -> tuple[Message, ...]:
        """Bracket regional dialogue with users for Gemma's chat template.

        These empty boundaries are only tokenizer context, never inference input.
        Their template cost is subtracted by PromptBuilder using the same counter.
        """
        return (Message(role="user", content=""), *messages, Message(role="user", content=""))

    async def __call__(self, messages: Sequence[Message], *, tools: Sequence[Tool] = ()) -> int:
        body: dict[str, Any] = {
            "messages": [message.model_dump(exclude_none=True) for message in messages]
        }
        if tools:
            body["tools"] = [tool.model_dump(exclude_none=True) for tool in tools]
        if self.tool_choice is not None:
            body["tool_choice"] = self.tool_choice
        try:
            async with local_http_client(self.timeout_seconds) as client:
                if self.before_send is not None:
                    await self.before_send(body)
                response = await client.post(self.base + "/apply-template", json=body)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("prompt"), str):
                    raise ValueError("Invalid template response")
                if self.before_send is not None:
                    await self.before_send(body)
                response = await client.post(
                    self.base + "/tokenize",
                    json={
                        "content": data["prompt"],
                        # Matches server's input_tokens path: tokenize_mixed(..., true, true).
                        "add_special": True,
                        "parse_special": True,
                        "with_pieces": False,
                    },
                )
                response.raise_for_status()
                data = response.json()
                tokens = data.get("tokens") if isinstance(data, dict) else None
                if not isinstance(tokens, list) or any(
                    type(token) is not int or token < 0 for token in tokens
                ):
                    raise ValueError("Invalid tokenizer response")
                return len(tokens)
        except httpx.TimeoutException:
            raise CoreError(504, "token_count_failed", "Token counting timed out") from None
        except (httpx.HTTPError, ValueError):
            raise CoreError(502, "token_count_failed", "Token counting failed") from None
