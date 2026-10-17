import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from digital_souls_core.character import Profile
from digital_souls_core.contracts import Message
from digital_souls_core.history import Binding, Operation, TurnInput

from .support import FakeProvider, chunk


class SyntheticPolicy:
    """Test-only policy for invented fixtures, not a production classifier."""

    denied: set[str]

    def __init__(self) -> None:
        self.denied = set()
        self.broken = False

    def allows(self, operation: Operation, binding: Binding, messages: tuple[Message, ...]) -> bool:
        if self.broken:
            raise ValueError("synthetic policy failure")
        return operation not in self.denied and "SYNTHETIC_SECRET" not in json.dumps(
            [m.model_dump() for m in messages]
        )


def turn(**values: Any) -> TurnInput:
    return TurnInput.model_validate(
        {
            "request_id": "r1",
            "expected_revision": 0,
            "messages": [{"role": "user", "content": "Synthetic hello"}],
            **values,
        }
    )


class PausedProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        self.started.set()
        await self.release.wait()
        return await super().complete(profile, payload)

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        try:
            yield chunk({"content": "Partial synthetic"})
            self.started.set()
            await self.release.wait()
            yield chunk({}, "stop")
        finally:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.closed = True
