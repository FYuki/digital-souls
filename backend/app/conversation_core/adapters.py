from __future__ import annotations

import asyncio
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
from app.screen_perception.provenance import ScreenLineage


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

    def mark_screen_derived(
        self, started_turn: object, lineages: tuple[ScreenLineage, ...]
    ) -> None:
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
        self._condition = threading.Condition()
        self._preparing = False
        self._active_transcriptions = 0
        self._pending_transcriptions = 0

    async def transcribe(self, audio: bytes) -> str:
        cancelled = threading.Event()
        with self._condition:
            self._pending_transcriptions += 1
        try:
            return await run_sync(self._transcribe_reserved, audio, cancelled)
        except asyncio.CancelledError:
            # 同期workerが準備完了を待っている間に破棄された音声は送信しない。
            cancelled.set()
            with self._condition:
                self._condition.notify_all()
            raise
        except SyncWorkerCapacityError as error:
            raise SttCapacityError("STT worker capacity exceeded") from error
        finally:
            with self._condition:
                self._pending_transcriptions -= 1

    def _transcribe_reserved(self, audio: bytes, cancelled: threading.Event) -> str:
        with self._condition:
            self._condition.wait_for(lambda: not self._preparing or cancelled.is_set())
            if cancelled.is_set():
                raise asyncio.CancelledError()
            if not self._capacity.acquire(blocking=False):
                raise SttCapacityError("STT capacity exceeded")
            self._active_transcriptions += 1
        try:
            return self._transcriber.transcribe(audio)
        finally:
            with self._condition:
                self._active_transcriptions -= 1
                self._capacity.release()

    async def prepare(self) -> bool:
        """認識要求がないときだけ準備し、失敗しても本来のSTTへ伝播させない。"""
        cancelled = threading.Event()
        try:
            return await run_sync(self._prepare_reserved, cancelled)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except Exception:
            return False

    def _prepare_reserved(self, cancelled: threading.Event) -> bool:
        prepare = getattr(self._transcriber, "prepare", None)
        if not callable(prepare):
            return False
        with self._condition:
            if (cancelled.is_set() or self._preparing or self._active_transcriptions
                    or self._pending_transcriptions):
                return False
            self._preparing = True
        try:
            return bool(prepare())
        finally:
            # await側がキャンセルされても、実HTTP要求が終わるまでは枠を保持する。
            with self._condition:
                self._preparing = False
                self._condition.notify_all()


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
        if not samples:
            raise ValueError("VOICEVOX WAV must contain audio frames")
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
    if sample_width == 2 and channels == 1:
        # VOICEVOXの標準PCM16 monoは、sampleごとのbytes生成・int変換を避ける。
        if len(pcm) % 2:
            raise ValueError("VOICEVOX PCM contains an incomplete frame")
        return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))
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
    if output_sample_rate == input_sample_rate * 2 and all(-32768 <= sample <= 32767 for sample in samples):
        # 24kHz→48kHzの線形補間は、元sampleと隣接sampleの中点を交互に置く。
        # 既存と同じ偶数丸め・末尾sample数を維持し、整数除算とclampの反復を省く。
        doubled = [0] * (len(samples) * 2 - 1)
        doubled[::2] = samples
        doubled[1::2] = [round((left + right) / 2) for left, right in zip(samples, samples[1:])]
        return doubled
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
            text = (await run_sync(self._generate_reply, transcript)).strip()
            if not text:
                raise ValueError("LLM response must not be empty")
            yield TextDelta(text_sequence=1, text=text, text_range=(0, len(text)))
            return
        sequence = 0
        offset = 0
        pending_whitespace = ""
        async for text in self._generate_stream(transcript):
            if not text:
                continue
            buffered = pending_whitespace + text
            if sequence == 0:
                # providerが本文前に返す改行は表示・TTSの対象にしない。
                buffered = buffered.lstrip()
            if not buffered or buffered.isspace():
                pending_whitespace = buffered
                continue
            content = buffered.rstrip()
            pending_whitespace = buffered[len(content) :]
            sequence += 1
            next_offset = offset + len(content)
            yield TextDelta(
                text_sequence=sequence,
                text=content,
                text_range=(offset, next_offset),
            )
            offset = next_offset
        if sequence == 0:
            raise ValueError("LLM response must not be empty")


class ScreenLineageResponseState:
    """単一Core session内のresponseと生成時lineageを対応付ける。"""

    def __init__(self) -> None:
        self._active_response_id: str | None = None
        self._lineages: dict[str, tuple[ScreenLineage, ...]] = {}

    def begin(self, response_id: str) -> None:
        self._active_response_id = response_id
        self._lineages[response_id] = ()

    def record(self, lineages: tuple[ScreenLineage, ...]) -> None:
        if self._active_response_id is None:
            raise RuntimeError("screen lineage response has not started")
        self._lineages[self._active_response_id] = lineages

    def finish(self, response_id: str) -> tuple[ScreenLineage, ...]:
        lineages = self._lineages.pop(response_id, ())
        if self._active_response_id == response_id:
            self._active_response_id = None
        return lineages


class ConversationHistoryPersistenceAdapter:
    def __init__(
        self,
        *,
        history_session: HistorySession,
        completed_turn_observer: Callable[[object], None] | None = None,
        screen_lineage_state: ScreenLineageResponseState | None = None,
    ) -> None:
        self._history_session = history_session
        self._completed_turn_observer = completed_turn_observer
        self._screen_lineage_state = screen_lineage_state
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
        if self._screen_lineage_state is not None:
            self._screen_lineage_state.begin(response_id)
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
        lineages = (
            ()
            if self._screen_lineage_state is None
            else self._screen_lineage_state.finish(outcome.response_id)
        )
        if lineages and outcome.state in {
            ResponseState.COMPLETED,
            ResponseState.CANCELLED,
        }:
            await run_sync(
                self._history_session.mark_screen_derived,
                started_turn,
                lineages,
            )
        if outcome.state is ResponseState.COMPLETED:
            persisted_turn = await run_sync(
                self._history_session.complete_turn,
                started_turn,
                outcome.generated_text,
            )
            if self._completed_turn_observer is not None:
                self._completed_turn_observer(persisted_turn)
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
