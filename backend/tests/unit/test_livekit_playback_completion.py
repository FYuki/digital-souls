from __future__ import annotations

import asyncio
import pytest
from app.livekit_transport.playback_completion import PlaybackCompletionGate


def test_full_confirmation_is_response_scoped_and_can_arrive_during_prepare():
    async def exercise():
        gate = PlaybackCompletionGate()
        async def prepare():
            assert not gate.confirm("old", 2)
            assert not gate.confirm("current", 1)
            assert gate.confirm("current", 2)
            assert not gate.confirm("current", 2)
        await gate.wait("current", 2, prepare)
        assert not gate.confirm("current", 2)
    asyncio.run(exercise())


def test_timeout_and_cancel_release_waiters_without_reusing_old_confirmation():
    async def exercise():
        gate = PlaybackCompletionGate(.01)
        async def prepare():
            return None
        with pytest.raises(TimeoutError):
            await gate.wait("timeout", 1, prepare)
        assert not gate.confirm("timeout", 1)
        pending = asyncio.create_task(gate.wait("cancelled", 1, prepare))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not gate.confirm("cancelled", 1)
    asyncio.run(exercise())


def test_duplicate_wait_cannot_replace_the_original_pending_response():
    async def exercise():
        gate = PlaybackCompletionGate()
        ready = asyncio.Event()
        async def prepare():
            ready.set()
        first = asyncio.create_task(gate.wait("current", 2, prepare))
        await ready.wait()
        with pytest.raises(ValueError):
            await gate.wait("current", 2, prepare)
        assert gate.confirm("current", 2)
        await first
    asyncio.run(exercise())
