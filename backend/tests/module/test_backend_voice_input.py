"""FE speech通知を使わず、実VADから正式utteranceを生成する。"""

from __future__ import annotations

import asyncio
from pathlib import Path
import wave

import numpy as np
import pytest

from app.voice_input.pipeline import AudioInputFault, VoiceInputPipeline
from app.voice_input.session import BackendVoiceInput


def speech_pcm() -> bytes:
    path = (
        Path(__file__).resolve().parents[3] / "frontend/playwright/fixtures/speech.wav"
    )
    with wave.open(str(path)) as audio:
        data = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        rate = audio.getframerate()
    samples = np.interp(
        np.arange(0, len(data), rate / 16000), np.arange(len(data)), data
    ).astype("<i2")
    return np.concatenate(
        (np.zeros(16000, dtype="<i2"), samples, np.zeros(32000, dtype="<i2"))
    ).tobytes()


def input_session():
    starts, stops, discards, frames = [], [], [], []

    async def stopped(boundary):
        stops.append(boundary)

    session = BackendVoiceInput(
        VoiceInputPipeline(),
        on_frame=frames.append,
        on_started=starts.append,
        on_stopped=stopped,
        on_discarded=lambda utterance, reason: discards.append((utterance, reason)),
    )
    return session, starts, stops, discards, frames


def test_real_vad_owns_utterance_without_client_speech_events() -> None:
    async def scenario() -> None:
        session, starts, stops, discards, _ = input_session()
        try:
            grant = await session.open(
                track_sid="mic-1", request_id="open-1", input_revision=1
            )
            pcm = speech_pcm()
            for offset in range(0, len(pcm), 320):
                await session.receive(
                    pcm[offset : offset + 320], start_sample=offset // 2, grant=grant
                )
            assert len(starts) == len(stops) == 1
            assert starts[0].utterance_id == stops[0].utterance_id
            assert starts[0].grant is grant
            assert (
                starts[0].detection.started_sample < starts[0].detection.detected_sample
            )
            assert (
                stops[0].detection.active_end_sample
                < stops[0].detection.detected_sample
            )
            assert not discards
        finally:
            await session.close()

    asyncio.run(scenario())


def test_old_track_cannot_be_reopened_or_feed_new_generation() -> None:
    async def scenario() -> None:
        session, _, _, _, frames = input_session()
        try:
            first = await session.open(
                track_sid="mic-1", request_id="open-1", input_revision=1
            )
            assert (
                await session.open(
                    track_sid="mic-1", request_id="open-1", input_revision=1
                )
                is first
            )
            assert session.suppress(reason="input_suppressed", input_revision=2)
            with pytest.raises(AudioInputFault, match="stale_input_request"):
                await session.open(
                    track_sid="mic-1", request_id="open-1", input_revision=1
                )
            with pytest.raises(AudioInputFault, match="new_microphone_track_required"):
                await session.open(
                    track_sid="mic-1", request_id="open-2", input_revision=3
                )
            second = await session.open(
                track_sid="mic-2", request_id="open-3", input_revision=3
            )
            assert second.input_generation > first.input_generation
            assert not session.suppress(reason="old mute", input_revision=2)
            await session.receive(bytes(3072), start_sample=0, grant=first)
            assert frames == []
            await session.receive(bytes(3072), start_sample=0, grant=second)
            assert len(frames) == 1
        finally:
            await session.close()

    asyncio.run(scenario())


def test_suppression_discards_unfinished_speech_but_not_finished_input() -> None:
    async def scenario() -> None:
        session, starts, stops, discards, _ = input_session()
        try:
            grant = await session.open(
                track_sid="mic-1", request_id="open-1", input_revision=1
            )
            pcm = speech_pcm()
            for offset in range(0, len(pcm), 320):
                await session.receive(
                    pcm[offset : offset + 320], start_sample=offset // 2, grant=grant
                )
                if starts:
                    break
            assert starts and not stops
            session.suppress(reason="input_suppressed", input_revision=2)
            assert discards == [(starts[0].utterance_id, "input_suppressed")]
            grant = await session.open(
                track_sid="mic-2", request_id="open-2", input_revision=3
            )
            for offset in range(0, len(pcm), 320):
                await session.receive(
                    pcm[offset : offset + 320], start_sample=offset // 2, grant=grant
                )
            assert len(stops) == 1
            session.suppress(reason="input_suppressed", input_revision=4)
            assert len(discards) == 1
            assert starts[0].utterance_id != starts[1].utterance_id
        finally:
            await session.close()

    asyncio.run(scenario())


def test_input_closed_during_open_reset_does_not_reopen() -> None:
    async def scenario() -> None:
        session, _, _, _, _ = input_session()
        started, release = asyncio.Event(), asyncio.Event()
        original = session._worker.reset

        async def slow_reset(*args, **kwargs):
            started.set()
            await release.wait()
            await original(*args, **kwargs)

        session._worker.reset = slow_reset
        try:
            opening = asyncio.create_task(
                session.open(track_sid="mic-1", request_id="open-1", input_revision=1)
            )
            await started.wait()
            session.suppress(reason="input_suppressed", input_revision=2)
            release.set()
            with pytest.raises(AudioInputFault, match="stale_input_request"):
                await opening
            assert session.grant is None
        finally:
            release.set()
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("active", [False, True])
def test_inference_failure_discards_input_and_recovers_after_silence(active):
    """実VADの途中障害を注入し、欠けた語尾を採用せず次の発話へ回復する。"""
    async def scenario():
        session, starts, stops, discards, frames = input_session()
        pipeline = session._worker._pipeline
        original = pipeline._model.process
        pcm = speech_pcm()
        offset = 0
        try:
            grant = await session.open(
                track_sid="mic-1", request_id="open-1", input_revision=1
            )
            if active:
                for offset in range(0, len(pcm), 320):
                    await session.receive(
                        pcm[offset:offset + 320], start_sample=offset // 2, grant=grant
                    )
                    if starts:
                        offset += 320
                        break
                assert starts and not stops
            def fail(samples):
                raise RuntimeError("injected inference failure")
            pipeline._model.process = fail
            await session.receive(
                pcm[offset:offset + 3072], start_sample=offset // 2, grant=grant
            )
            assert discards == [
                (starts[0].utterance_id if active else None, "vad_processing_failed")
            ]
            assert session.grant is grant
            pipeline._model.process = original
            position = offset // 2 + 1536
            # 欠落後の連続した語尾は、強い音声でも新しい発話として採用しない。
            before = len(starts)
            loud = np.full(1536, 8000, dtype="<i2").tobytes()
            for _ in range(12):
                await session.receive(loud, start_sample=position, grant=grant)
                position += 1536
            assert len(starts) == before
            assert not stops
            for offset in range(0, len(pcm), 320):
                await session.receive(
                    pcm[offset:offset + 320],
                    start_sample=position + offset // 2, grant=grant,
                )
            assert len(starts) == before + 1
            assert len(stops) == 1
            assert stops[0].utterance_id == starts[-1].utterance_id
        finally:
            pipeline._model.process = original
            await session.close()
    asyncio.run(scenario())


def test_open_reset_failure_is_typed_and_new_track_can_retry():
    async def scenario():
        session, _, _, _, _ = input_session()
        pipeline = session._worker._pipeline
        original = pipeline.reset
        def fail(*args, **kwargs):
            raise RuntimeError("injected reset failure")
        try:
            pipeline.reset = fail
            with pytest.raises(AudioInputFault, match="vad_unavailable"):
                await session.open(
                    track_sid="mic-1", request_id="open-1", input_revision=1
                )
            assert session.grant is None
            pipeline.reset = original
            grant = await session.open(
                track_sid="mic-2", request_id="open-2", input_revision=2
            )
            assert session.grant is grant
        finally:
            pipeline.reset = original
            await session.close()
    asyncio.run(scenario())


def test_failed_recovery_revokes_grant_and_requires_reopen():
    async def scenario():
        session, _, _, discards, frames = input_session()
        pipeline = session._worker._pipeline
        original_feed, original_reset = pipeline.feed, pipeline.reset
        def fail(*args, **kwargs):
            raise RuntimeError("injected failure")
        try:
            grant = await session.open(
                track_sid="mic-1", request_id="open-1", input_revision=1
            )
            pipeline.feed = pipeline.reset = fail
            with pytest.raises(AudioInputFault, match="vad_unavailable"):
                await session.receive(bytes(3072), start_sample=0, grant=grant)
            assert session.grant is None
            pipeline.feed, pipeline.reset = original_feed, original_reset
            await session.receive(bytes(3072), start_sample=1536, grant=grant)
            assert not frames
            next_grant = await session.open(
                track_sid="mic-2", request_id="open-2", input_revision=2
            )
            await session.receive(bytes(3072), start_sample=0, grant=next_grant)
            assert len(frames) == 1
        finally:
            pipeline.feed, pipeline.reset = original_feed, original_reset
            await session.close()
    asyncio.run(scenario())


def test_late_failed_recovery_does_not_revoke_new_input():
    async def scenario():
        session, _, _, _, frames = input_session()
        pipeline = session._worker._pipeline
        original_feed, original_reset = pipeline.feed, session._worker.reset
        entered, release = asyncio.Event(), asyncio.Event()
        def fail(*args, **kwargs):
            raise RuntimeError("injected inference failure")
        async def delayed_reset(*args, **kwargs):
            if kwargs.get("quarantine"):
                entered.set()
                await release.wait()
                raise AudioInputFault("vad_unavailable")
            await original_reset(*args, **kwargs)
        receiving = None
        try:
            first = await session.open(
                track_sid="mic-1", request_id="open-1", input_revision=1
            )
            pipeline.feed = fail
            session._worker.reset = delayed_reset
            receiving = asyncio.create_task(
                session.receive(bytes(3072), start_sample=0, grant=first)
            )
            await asyncio.wait_for(entered.wait(), 2)
            pipeline.feed = original_feed
            second = await session.open(
                track_sid="mic-2", request_id="open-2", input_revision=2
            )
            release.set()
            await receiving
            assert session.grant is second
            await session.receive(bytes(3072), start_sample=0, grant=second)
            assert len(frames) == 1
        finally:
            release.set()
            if receiving is not None:
                await asyncio.gather(receiving, return_exceptions=True)
            pipeline.feed = original_feed
            session._worker.reset = original_reset
            await session.close()
    asyncio.run(scenario())
