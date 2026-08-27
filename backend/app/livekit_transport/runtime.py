from __future__ import annotations

from typing import Protocol


class ObservationPort(Protocol):
    def record(self, observation: dict[str, object]) -> None: ...


class MicrophoneTrackObserver:
    def __init__(self, *, observation_port: ObservationPort) -> None:
        self._port = observation_port
        self._frame_count = 0
        self._sample_count = 0
        self._first_received_at_ms: int | None = None
        self._last_received_at_ms: int | None = None
        self._last_frame_samples: int | None = None
        self._missing_frames = 0

    def receive_frame(self, *, pcm: bytes, sample_count: int, received_at_ms: int) -> None:
        del pcm
        self._frame_count += 1
        self._sample_count += sample_count
        if self._first_received_at_ms is None:
            self._first_received_at_ms = received_at_ms
        previous_received_at_ms = self._last_received_at_ms
        previous_samples = self._last_frame_samples
        self._last_received_at_ms = received_at_ms
        self._last_frame_samples = sample_count
        if self._frame_count > 1:
            assert previous_received_at_ms is not None
            assert previous_samples is not None
            expected_interval_ms = previous_samples * 1000 / 48_000
            elapsed_since_frame = received_at_ms - previous_received_at_ms
            self._missing_frames += max(
                0, round(elapsed_since_frame / expected_interval_ms) - 1
            )
            self._port.record(
                {
                    "frame_count": self._frame_count,
                    "sample_count": self._sample_count,
                    "elapsed_ms": self._last_received_at_ms - self._first_received_at_ms,
                    "missing_frames": self._missing_frames,
                }
            )


class AudioSource(Protocol):
    async def publish(self, pcm: bytes) -> None: ...


class SegmentMetadataPort(Protocol):
    async def publish(self, metadata: dict[str, object]) -> None: ...


class PcmFixturePublisher:
    def __init__(
        self,
        *,
        audio_source: AudioSource,
        metadata_port: SegmentMetadataPort,
    ) -> None:
        self._audio_source = audio_source
        self._metadata_port = metadata_port

    async def publish(
        self,
        *,
        response_id: str,
        audio_sequence: int,
        generation: int,
        pcm: bytes,
        sample_count: int,
    ) -> None:
        metadata = {
            "type": "logical_audio_segment",
            "response_id": response_id,
            "audio_sequence": audio_sequence,
            "generation": generation,
            "pcm_sample_count": sample_count,
        }
        await self._metadata_port.publish(metadata)
        await self._audio_source.publish(pcm)
