from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Awaitable
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import AsyncMock, Mock

import pytest

from app.conversation_core import CoreEvent
from app.livekit_transport import (
    core_delivery,
    core_factory,
    microphone_bridge,
    microphone_reader,
    production_sdk,
    session_runtime,
)
from app.livekit_transport.bootstrap import BootstrapTimeoutError

from tests.conversation_core_test_support import make_pcm16_wav
from tests.livekit_session_test_support import runtime_shell, session_owner
from tests.voice_capture_test_support import begin_capture, finish_capture, capture_harness

# LiveKit音声経路の変更対象外契約値。module移動ではなく値そのものを固定する。
PCM_SAMPLE_RATE = 48_000
PCM_CHANNELS = 1
STT_TURN_PREVIEW_PCM_BYTES = int(16_000 * 2 * 0.8)
STT_MICROPHONE_PREROLL_BYTES = 16_000 * 2 * 2
STT_MAX_OPEN_CAPTURES = 4


def _runtime_module(contract: str):
    module_name = "app.livekit_transport.runtime"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is None or not (
            error.name == module_name or module_name.startswith(f"{error.name}.")
        ):
            raise
    pytest.fail(f"{module_name} must implement {contract}")


@dataclass
class RecordingObservationPort:
    records: list[dict[str, object]] = field(default_factory=list)

    def record(self, observation: dict[str, object]) -> None:
        self.records.append(observation)


class NoopCoreSession:
    async def end(self) -> None:
        return None


class NoopCoreSessionFactory:
    def create(self, **_request: object) -> NoopCoreSession:
        return NoopCoreSession()


async def _drain_asyncio_tasks(tasks: set[asyncio.Task[None]]) -> None:
    """done callbackが次taskを作る直列queueをevent-loop turn単位で待つ。"""
    for _ in range(100):
        await asyncio.sleep(0)
        if not tasks:
            await asyncio.sleep(0)
            if not tasks:
                return
    raise AssertionError("asyncio tasks did not drain")
