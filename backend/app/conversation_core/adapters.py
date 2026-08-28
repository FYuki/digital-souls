from __future__ import annotations

import wave
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from io import BytesIO
import struct
import threading
from typing import Protocol, cast

from app.async_worker import SyncWorkerCapacityError, run_sync

from app.conversation_core.models import (
    AudioSegment,
    ResponseStartResult,
    ResponseState,
    TerminalOutcome,
    TextDelta,
)


class SyncTranscriber(Protocol):
    def transcribe(self, audio: bytes) -> str:
        ...


class SingleArgumentSynthesizer(Protocol):
    def synthesize(self, text: str) -> bytes:
        ...


class SpeakerSynthesizer(Protocol):
    def synthesize(self, text: str, speaker_id: int) -> bytes:
        ...


class HistorySession(Protocol):
    def start_turn(self, user_content: str) -> object:
        ...

    def complete_turn(self, started_turn: object, assistant_content: str) -> object:
        ...

    def interrupt_turn(
        self,
        started_turn: object,
        generated_text: str,
        response_audio_segments: Sequence[Mapping[str, object]],
        last_played_audio_sequence: int,
    ) -> object:
        ...

    def fail_turn(self, started_turn: object) -> None:
        ...


_HISTORY_START_FAILED = object()


class SttCapacityError(RuntimeError):
    """STT隔離workerへ安全に投入できる同時実行上限を超えた。"""

    error_code = "stt_capacity_exceeded"


class WhisperSttAdapter:
    def __init__(
        self, *, transcriber: SyncTranscriber, max_inflight: int = 1
    ) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight must be positive")
        self._transcriber = transcriber
        self._capacity = threading.BoundedSemaphore(max_inflight)

    async def transcribe(self, audio: bytes) -> str:
        try:
            return await run_sync(self._transcribe_reserved, audio)
        except SyncWorkerCapacityError as error:
            raise SttCapacityError("STT worker capacity exceeded") from error

    def _transcribe_reserved(self, audio: bytes) -> str:
        if not self._capacity.acquire(blocking=False):
            raise SttCapacityError("STT capacity exceeded")
        try:
            return self._transcriber.transcribe(audio)
        finally:
            self._capacity.release()


class VoicevoxTtsAdapter:
    def __init__(
        self,
        *,
        client: SingleArgumentSynthesizer | SpeakerSynthesizer,
        output_sample_rate: int,
        output_channels: int,
        output_sample_width: int,
        speaker_id: int | None = None,
    ) -> None:
        if output_sample_rate <= 0:
            raise ValueError("output_sample_rate must be positive")
        if output_channels != 1:
            raise ValueError("VoicevoxTtsAdapter output must be mono")
        if output_sample_width != 2:
            raise ValueError("VoicevoxTtsAdapter output must be PCM16")
        self._client = client
        self._speaker_id = speaker_id
        self._output_sample_rate = output_sample_rate
        self._output_channels = output_channels
        self._output_sample_width = output_sample_width

    async def synthesize(self, text: str) -> AsyncIterator[AudioSegment]:
        audio = await run_sync(self._synthesize_pcm, text)
        yield AudioSegment(audio_sequence=1, audio=audio, text_range=(0, len(text)))

    def _synthesize_pcm(self, text: str) -> bytes:
        if self._speaker_id is None:
            synthesize = cast(SingleArgumentSynthesizer, self._client).synthesize
            wav_audio = synthesize(text)
        else:
            synthesize_with_speaker = cast(
                SpeakerSynthesizer, self._client
            ).synthesize
            wav_audio = synthesize_with_speaker(text, self._speaker_id)
        return self._normalize_wav(wav_audio)

    def _normalize_wav(self, wav_audio: bytes) -> bytes:
        with wave.open(BytesIO(wav_audio), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE":
                raise ValueError("VOICEVOX WAV must contain uncompressed PCM")
            input_channels = wav_file.getnchannels()
            input_sample_width = wav_file.getsampwidth()
            input_sample_rate = wav_file.getframerate()
            if input_channels not in {1, 2}:
                raise ValueError("VOICEVOX WAV must have one or two channels")
            if input_sample_width not in {1, 2, 3, 4}:
                raise ValueError("VOICEVOX WAV has an unsupported sample width")
            pcm = wav_file.readframes(wav_file.getnframes())

        samples = _decode_mono_pcm16(
            pcm,
            sample_width=input_sample_width,
            channels=input_channels,
        )
        if input_sample_rate != self._output_sample_rate:
            samples = _resample_pcm16(
                samples,
                input_sample_rate=input_sample_rate,
                output_sample_rate=self._output_sample_rate,
            )
        return struct.pack(f"<{len(samples)}h", *samples)


def _decode_mono_pcm16(
    pcm: bytes, *, sample_width: int, channels: int
) -> list[int]:
    frame_width = sample_width * channels
    samples: list[int] = []
    for frame_offset in range(0, len(pcm), frame_width):
        channels_in_frame: list[int] = []
        for channel in range(channels):
            offset = frame_offset + channel * sample_width
            encoded = pcm[offset : offset + sample_width]
            if len(encoded) != sample_width:
                raise ValueError("VOICEVOX PCM contains an incomplete frame")
            if sample_width == 1:
                value = (encoded[0] - 128) << 8
            else:
                raw = int.from_bytes(encoded, "little", signed=True)
                value = raw >> (8 * (sample_width - 2))
            channels_in_frame.append(value)
        mixed = round(sum(channels_in_frame) / len(channels_in_frame))
        samples.append(max(-32_768, min(32_767, mixed)))
    return samples


def _resample_pcm16(
    samples: list[int], *, input_sample_rate: int, output_sample_rate: int
) -> list[int]:
    if len(samples) < 2:
        return list(samples)
    output_count = ((len(samples) - 1) * output_sample_rate) // input_sample_rate + 1
    output: list[int] = []
    for output_index in range(output_count):
        numerator = output_index * input_sample_rate
        left = numerator // output_sample_rate
        remainder = numerator % output_sample_rate
        if left >= len(samples) - 1:
            output.append(samples[-1])
            continue
        ratio = remainder / output_sample_rate
        interpolated = round(samples[left] * (1 - ratio) + samples[left + 1] * ratio)
        output.append(max(-32_768, min(32_767, interpolated)))
    return output


class PromptLlmAdapter:
    def __init__(
        self,
        *,
        generate_reply: Callable[[str], str] | None = None,
        generate_stream: Callable[[str], AsyncIterator[str]] | None = None,
    ) -> None:
        if (generate_reply is None) == (generate_stream is None):
            raise ValueError("exactly one LLM generation source is required")
        self._generate_reply = generate_reply
        self._generate_stream = generate_stream

    async def generate(self, transcript: str) -> AsyncIterator[TextDelta]:
        if self._generate_stream is None:
            if self._generate_reply is None:
                raise RuntimeError("LLM generation source is missing")
            text = await run_sync(self._generate_reply, transcript)
            if not text:
                raise ValueError("LLM response must not be empty")
            yield TextDelta(text_sequence=1, text=text, text_range=(0, len(text)))
            return
        sequence = 0
        offset = 0
        async for text in self._generate_stream(transcript):
            if not text:
                continue
            sequence += 1
            next_offset = offset + len(text)
            yield TextDelta(
                text_sequence=sequence,
                text=text,
                text_range=(offset, next_offset),
            )
            offset = next_offset
        if sequence == 0:
            raise ValueError("LLM response must not be empty")


class ConversationHistoryPersistenceAdapter:
    def __init__(self, *, history_session: HistorySession) -> None:
        self._history_session = history_session
        self._history_turns: dict[str, object] = {}
        self._persisted_response_ids: set[str] = set()

    async def start_response(
        self, *, response_id: str, user_content: str
    ) -> ResponseStartResult:
        if response_id in self._history_turns:
            raise ValueError("response history turn has already started")
        try:
            started_turn = await run_sync(
                self._history_session.start_turn,
                user_content,
            )
        except Exception:
            self._history_turns[response_id] = _HISTORY_START_FAILED
            raise
        self._history_turns[response_id] = started_turn
        content_skipped = getattr(started_turn, "content_skipped", None)
        if not isinstance(content_skipped, bool):
            raise TypeError("started history turn must expose content_skipped")
        return ResponseStartResult(content_skipped=content_skipped)

    async def persist(self, outcome: TerminalOutcome) -> None:
        if outcome.response_id in self._persisted_response_ids:
            return
        started_turn = self._history_turns[outcome.response_id]
        if started_turn is _HISTORY_START_FAILED:
            if outcome.state is not ResponseState.FAILED:
                raise ValueError(
                    "response with failed history start must terminate as failed"
                )
            self._persisted_response_ids.add(outcome.response_id)
            return
        self._persisted_response_ids.add(outcome.response_id)
        if outcome.state is ResponseState.COMPLETED:
            await run_sync(
                self._history_session.complete_turn,
                started_turn,
                outcome.generated_text,
            )
            return
        if outcome.state is ResponseState.CANCELLED:
            segments = [
                {
                    "audio_sequence": segment.audio_sequence,
                    "text_range": {
                        "start": segment.text_range[0],
                        "end": segment.text_range[1],
                    },
                }
                for segment in outcome.audio_segments
            ]
            await run_sync(
                self._history_session.interrupt_turn,
                started_turn,
                outcome.generated_text,
                segments,
                outcome.last_played_audio_sequence,
            )
            return
        await run_sync(self._history_session.fail_turn, started_turn)
