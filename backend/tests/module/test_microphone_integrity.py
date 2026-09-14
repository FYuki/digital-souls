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
