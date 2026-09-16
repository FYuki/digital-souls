"""packet loss・jitterを単独で欠落認定せず、統計の未観測も成功にしない。"""

from __future__ import annotations

import asyncio
import pytest
from livekit.rtc._proto import stats_pb2 as stats

from app.livekit_transport.microphone_integrity import MicrophoneIntegrity
from app.voice_input.pipeline import AudioInputFault


def report(timestamp: int, concealed: int = 0, silent: int = 0):
    return [
        stats.RtcStats(
            codec=stats.RtcStats.Codec(
                rtc=stats.RtcStatsData(id="codec"),
                codec=stats.CodecStats(mime_type="audio/opus", clock_rate=48000),
            )
        ),
        stats.RtcStats(
            inbound_rtp=stats.RtcStats.InboundRtp(
                rtc=stats.RtcStatsData(id="inbound", timestamp=timestamp),
                stream=stats.RtpStreamStats(kind="audio", codec_id="codec"),
                received=stats.ReceivedRtpStreamStats(packets_lost=100, jitter=0.3),
                inbound=stats.InboundRtpStreamStats(
                    concealed_samples=concealed, silent_concealed_samples=silent
                ),
            )
        ),
    ]


@pytest.mark.parametrize(
    "concealed,silent,lost",
    [(0, 0, False), (4800, 4800, False), (3839, 0, False), (3840, 0, True)],
)
def test_only_non_silent_concealment_at_threshold_discards(
    concealed, silent, lost
) -> None:
    async def scenario() -> None:
        position, timestamp = 0, 0
        current_concealed = current_silent = 0

        async def read():
            nonlocal timestamp
            timestamp += 1
            return report(timestamp, current_concealed, current_silent)

        monitor = MicrophoneIntegrity(read, lambda: position)
        await monitor.observe()
        position, current_concealed, current_silent = 1600, concealed, silent
        if lost:
            with pytest.raises(AudioInputFault, match="audio_gap"):
                await monitor.verify(0, 1600)
        else:
            await monitor.verify(0, 1600)
        monitor.close()

    asyncio.run(scenario())


def test_initial_unobserved_interval_and_stale_stats_are_not_success(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.livekit_transport.microphone_integrity.VERIFY_TIMEOUT_SECONDS", 0.02
    )

    async def scenario() -> None:
        async def read():
            return report(1)

        monitor = MicrophoneIntegrity(read, lambda: 1600)
        await monitor.observe()
        with pytest.raises(AudioInputFault, match="audio_integrity_unavailable"):
            await monitor.verify(0, 1600)
        with pytest.raises(AudioInputFault, match="audio_integrity_unavailable"):
            await monitor.observe()
        monitor.close()

    asyncio.run(scenario())


def test_missing_stats_fields_and_wrong_codec_are_not_zero_loss() -> None:
    from app.livekit_transport.microphone_integrity import _counters

    missing = report(1)
    missing[1].inbound_rtp.inbound.ClearField("concealed_samples")
    with pytest.raises(AudioInputFault, match="audio_integrity_unavailable"):
        _counters(missing)
    wrong_rate = report(1)
    wrong_rate[0].codec.codec.clock_rate = 16000
    with pytest.raises(AudioInputFault, match="audio_integrity_unavailable"):
        _counters(wrong_rate)


def test_samples_arriving_during_stats_request_are_not_covered() -> None:
    async def scenario() -> None:
        position = 0

        async def read():
            nonlocal position
            position = 1600
            return report(1)

        monitor = MicrophoneIntegrity(read, lambda: position)
        await monitor.observe()
        assert monitor._covered_end == 0
        monitor.close()

    asyncio.run(scenario())


def test_verified_window_survives_a_cached_duplicate_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.livekit_transport.microphone_integrity.VERIFY_TIMEOUT_SECONDS", 0.02
    )

    async def scenario() -> None:
        position, timestamp = 0, 1

        async def read():
            return report(timestamp)

        monitor = MicrophoneIntegrity(read, lambda: position)
        await monitor.observe()
        position, timestamp = 1600, 2
        # background pollが先に確認した範囲を、verify自身の再読込成功に依存させない。
        await monitor.observe()
        await monitor.verify(0, 1600)
        # 同じsnapshotを、新たに届いたPCMの証拠として延長することは禁止。
        position = 3200
        with pytest.raises(AudioInputFault, match="audio_integrity_unavailable"):
            await monitor.verify(0, 3200)
        monitor.close()
        with pytest.raises(AudioInputFault, match="audio_integrity_unavailable"):
            await monitor.verify(0, 1600)

    asyncio.run(scenario())


def test_cached_verified_window_keeps_detected_loss() -> None:
    async def scenario() -> None:
        position, timestamp, concealed = 0, 1, 0

        async def read():
            return report(timestamp, concealed)

        monitor = MicrophoneIntegrity(read, lambda: position)
        await monitor.observe()
        position, timestamp, concealed = 1600, 2, 3840
        await monitor.observe()
        with pytest.raises(AudioInputFault, match="audio_gap"):
            await monitor.verify(0, 1600)
        monitor.close()

    asyncio.run(scenario())


def test_input_readiness_waits_for_valid_initial_stats_without_backfilling():
    async def scenario():
        position = 1600
        valid = False
        async def read():
            return report(1) if valid else []
        monitor = MicrophoneIntegrity(read, lambda: position)
        waiting = asyncio.create_task(monitor.wait_ready())
        await asyncio.sleep(0)
        assert not waiting.done()
        with pytest.raises(AudioInputFault):
            await monitor.observe()
        assert not waiting.done()
        valid = True
        await monitor.observe()
        await waiting
        assert monitor._known_start == 1600
        assert not monitor._verified_range(0, 1600)
        monitor.close()
    asyncio.run(scenario())


def test_reader_close_releases_pending_readiness_as_unavailable():
    async def scenario():
        async def read():
            return []
        monitor = MicrophoneIntegrity(read, lambda: 0)
        waiting = asyncio.create_task(monitor.wait_ready())
        await asyncio.sleep(0)
        monitor.close()
        with pytest.raises(AudioInputFault, match="audio_integrity_unavailable"):
            await waiting
    asyncio.run(scenario())


def test_cancelled_readiness_does_not_cancel_statistics_monitor():
    async def scenario():
        async def read():
            return report(1)
        monitor = MicrophoneIntegrity(read, lambda: 0)
        waiting = asyncio.create_task(monitor.wait_ready())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await monitor.observe()
        await monitor.wait_ready()
        monitor.close()
    asyncio.run(scenario())


def test_integrity_timeout_exposes_numeric_range_and_stale_snapshot_counts(monkeypatch):
    monkeypatch.setattr("app.livekit_transport.microphone_integrity.VERIFY_TIMEOUT_SECONDS", 0.02)
    async def scenario():
        from app.livekit_transport.microphone_integrity import AudioIntegrityFault
        position = 1600
        async def read():
            return report(1)
        monitor = MicrophoneIntegrity(read, lambda: position)
        await monitor.observe()
        position = 3200
        with pytest.raises(AudioIntegrityFault) as captured:
            await monitor.verify(0, 3200)
        values = captured.value.statistics
        assert all(type(value) is int and value >= 0 for value in values.values())
        assert values["input_integrity_known_start_sample"] == 1600
        assert values["input_integrity_covered_end_sample"] == 1600
        assert values["input_integrity_requested_start_sample"] == 0
        assert values["input_integrity_requested_end_sample"] == 3200
        assert values["input_integrity_observations"] == 1
        assert values["input_integrity_stale_snapshots"] >= 1
        monitor.close()
    asyncio.run(scenario())
