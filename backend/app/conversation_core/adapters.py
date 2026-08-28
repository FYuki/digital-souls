from __future__ import annotations

import audioop
import asyncio
import wave
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from io import BytesIO
from typing import Protocol, cast

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


class WhisperSttAdapter:
    def __init__(self, *, transcriber: SyncTranscriber) -> None:
        self._transcriber = transcriber

    async def transcribe(self, audio: bytes) -> str:
        return await asyncio.to_thread(self._transcriber.transcribe, audio)


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
        audio = await asyncio.to_thread(self._synthesize_pcm, text)
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

        if input_sample_width == 1:
            pcm = audioop.bias(pcm, input_sample_width, -128)
        if input_channels == 2:
            pcm = audioop.tomono(pcm, input_sample_width, 0.5, 0.5)
        if input_sample_width != self._output_sample_width:
            pcm = audioop.lin2lin(
                pcm,
                input_sample_width,
                self._output_sample_width,
            )
        if input_sample_rate != self._output_sample_rate:
            pcm, _state = audioop.ratecv(
                pcm,
                self._output_sample_width,
                self._output_channels,
                input_sample_rate,
                self._output_sample_rate,
                None,
            )
        return pcm


class PromptLlmAdapter:
    def __init__(self, *, generate_reply: Callable[[str], str]) -> None:
        self._generate_reply = generate_reply

    async def generate(self, transcript: str) -> AsyncIterator[TextDelta]:
        text = await asyncio.to_thread(self._generate_reply, transcript)
        yield TextDelta(text_sequence=1, text=text, text_range=(0, len(text)))


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
            started_turn = await asyncio.to_thread(
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
            await asyncio.to_thread(
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
            await asyncio.to_thread(
                self._history_session.interrupt_turn,
                started_turn,
                outcome.generated_text,
                segments,
                outcome.last_played_audio_sequence,
            )
            return
        await asyncio.to_thread(self._history_session.fail_turn, started_turn)
