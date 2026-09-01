from __future__ import annotations

from typing import Protocol


class ObservationPort(Protocol):
    def record(self, observation: dict[str, object]) -> None: ...


class MicrophoneTrackObserver:
    def __init__(
        self,
        *,
        observation_port: ObservationPort,
        sample_rate: int = 48_000,
        observation_interval_ms: int = 1_000,
    ) -> None:
        if observation_interval_ms < 1:
            raise ValueError("observation_interval_ms must be positive")
        self._port = observation_port
        self._sample_rate = sample_rate
        self._observation_interval_ms = observation_interval_ms
        self._frame_count = 0
        self._sample_count = 0
        self._first_received_at_ms: int | None = None
        self._last_received_at_ms: int | None = None
        self._last_observed_at_ms: int | None = None
        self._last_frame_samples: int | None = None
        self._missing_frames = 0

    def receive_frame(self, *, pcm: bytes, sample_count: int, received_at_ms: int) -> None:
        del pcm
        self._frame_count += 1
        self._sample_count += sample_count
        if self._first_received_at_ms is None:
            self._first_received_at_ms = received_at_ms
            self._last_observed_at_ms = received_at_ms
        previous_received_at_ms = self._last_received_at_ms
        previous_samples = self._last_frame_samples
        self._last_received_at_ms = received_at_ms
        self._last_frame_samples = sample_count
        if self._frame_count > 1:
            assert previous_received_at_ms is not None
            assert previous_samples is not None
            expected_interval_ms = previous_samples * 1000 / self._sample_rate
            elapsed_since_frame = received_at_ms - previous_received_at_ms
            self._missing_frames += max(
                0, round(elapsed_since_frame / expected_interval_ms) - 1
            )
            assert self._last_observed_at_ms is not None
            if (
                received_at_ms - self._last_observed_at_ms
                < self._observation_interval_ms
            ):
                return
            self._last_observed_at_ms = received_at_ms
            self._port.record(
                {
                    "frame_count": self._frame_count,
                    "sample_count": self._sample_count,
                    "elapsed_ms": self._last_received_at_ms - self._first_received_at_ms,
                    "missing_frames": self._missing_frames,
                }
            )
