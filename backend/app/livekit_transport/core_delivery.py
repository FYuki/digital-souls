"""Conversation Core eventの配送とwire payload変換を所有する。"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from app.conversation_core import CoreEvent
from app.conversation_core.models import Response, ResponseStopResult
from app.livekit_transport.coordinator import (
    ConfirmedOutputStop,
    ProductionSessionCoordinator,
)
from app.livekit_transport.measurement import LiveKitMeasurementSession
from app.livekit_transport.playback_completion import PlaybackCompletionGate
from app.livekit_transport.production_sdk import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH_BYTES,
    livekit_rtc_module,
)
from app.livekit_transport.response_audio import ResponseAudioTracks

if TYPE_CHECKING:
    import livekit.rtc as rtc


class ProductionCoreEventInbox:
    """Conversation Core が同一process内で消費する検証済みevent入口。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[bytes], None]] = {}
        self._pending_by_session: dict[str, deque[bytes]] = {}

    def notify(self, payload: bytes) -> None:
        value = json.loads(payload)
        session_id = value.get("session_id") if isinstance(value, dict) else None
        if isinstance(session_id, str):
            handler = self._handlers.get(session_id)
            if handler is not None:
                handler(payload)
                return
            self._pending_by_session.setdefault(session_id, deque()).append(payload)

    def bind(self, session_id: str, handler: Callable[[bytes], None]) -> None:
        if session_id in self._handlers:
            raise RuntimeError("Core event handler is already bound")
        self._handlers[session_id] = handler
        pending = self._pending_by_session.get(session_id)
        while pending:
            handler(pending.popleft())
        self._pending_by_session.pop(session_id, None)

    def unbind(self, session_id: str) -> None:
        self._handlers.pop(session_id, None)
        self._pending_by_session.pop(session_id, None)


class _RtcAudioSource(Protocol):
    async def capture_frame(self, frame: rtc.AudioFrame) -> None: ...

    def clear_queue(self) -> None: ...


class _LiveKitPcmAudioSource:
    def __init__(self, source: _RtcAudioSource) -> None:
        self._source = source

    async def begin_response(self, response_id: str) -> None:
        """単一sourceのadapter。productionではResponseAudioTracksがtrackを分離する。"""

    async def publish(self, pcm: bytes, *, response_id: str) -> int:
        rtc_module = livekit_rtc_module()

        samples_per_channel = len(pcm) // (
            PCM_SAMPLE_WIDTH_BYTES * PCM_CHANNELS
        )
        frame: rtc.AudioFrame = rtc_module.AudioFrame(
            pcm,
            PCM_SAMPLE_RATE,
            PCM_CHANNELS,
            samples_per_channel,
        )
        await self._source.capture_frame(frame)
        return time.monotonic_ns()

    async def finish_response(self, response_id: str) -> None:
        """既存adapterのnative bufferは従来どおりsourceが所有する。"""

    def clear(self, response_id: str | None = None) -> None:
        self._source.clear_queue()


class _ConversationCoreDelivery:
    def __init__(
        self,
        *,
        coordinator: ProductionSessionCoordinator,
        audio_source: _LiveKitPcmAudioSource | ResponseAudioTracks,
        character_participant_id: str,
        character_id: str,
        user_participant_id: str | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._audio_source = audio_source
        self._character_speaker = {
            "participant_id": character_participant_id,
            "role": "character",
            "character_id": character_id,
        }
        self._user_speaker = (
            {"participant_id": user_participant_id, "role": "user"}
            if user_participant_id is not None
            else None
        )
        self._session_id: str | None = None
        self._completion_gate = PlaybackCompletionGate()
        self._completed_output_responses: set[str] = set()
        self._first_audio_observed: set[str] = set()
        self._first_text_observed: set[str] = set()
        self._measurement: LiveKitMeasurementSession | None = None

    @property
    def measurement(self) -> LiveKitMeasurementSession | None:
        return self._measurement

    def attach_measurement(self, measurement: LiveKitMeasurementSession) -> None:
        if self._measurement is measurement:
            return
        if self._measurement is not None and self._measurement is not measurement:
            raise RuntimeError("delivery measurement is already attached")
        self._measurement = measurement
        if isinstance(self._audio_source, ResponseAudioTracks):
            self._audio_source.set_observer(lambda name, response_id: measurement.record_response_event(
                response_id=response_id, name=name, stage="transport",
            ))

    async def publish(self, event: CoreEvent) -> None:
        if self._session_id is None:
            self._session_id = event.session_id
        elif self._session_id != event.session_id:
            raise ValueError("delivery event belongs to another session")
        if (
            event.response_id is not None
            and event.source_utterance_ids is not None
            and self._measurement is not None
        ):
            self._measurement.bind_response(
                response_id=event.response_id,
                source_utterance_ids=event.source_utterance_ids,
                source_inputs=event.source_inputs,
            )
        if event.type == "response_started":
            if event.response_id is None or event.source_utterance_ids is None:
                raise ValueError(
                    "response_started requires response id and source utterances"
                )
            if self._measurement is not None:
                self._measurement.bind_response(
                    response_id=event.response_id,
                    source_utterance_ids=event.source_utterance_ids,
                    source_inputs=event.source_inputs,
                )
                self._measurement.record_response_event(
                    response_id=event.response_id,
                    name="response_decision",
                    stage="turn",
                )
            self._coordinator.begin_response(response_id=event.response_id)
            await self._audio_source.begin_response(event.response_id)
        if (
            event.type == "utterance_finalized"
            and event.utterance_id is not None
            and self._measurement is not None
        ):
            self._measurement.record_utterance_event(
                utterance_id=event.utterance_id,
                name="utterance_finalized",
                stage="stt",
            )
            if event.should_response is False:
                self._measurement.bind_utterance_outcome(
                    utterance_id=event.utterance_id,
                    outcome="excluded",
                    reason_code="response_not_requested",
                )
        if (
            event.type in {"utterance_discarded", "error"}
            and event.utterance_id is not None
            and self._measurement is not None
        ):
            outcome: Literal["failure", "excluded"] = (
                "failure" if event.type == "error" else "excluded"
            )
            reason_code = (
                event.error_code if event.type == "error" else event.reason
            )
            self._measurement.bind_utterance_outcome(
                utterance_id=event.utterance_id,
                outcome=outcome,
                reason_code=reason_code or f"{event.type}_without_reason",
            )
        if (
            event.type == "response_delta"
            and event.response_id is not None
            and event.response_id not in self._first_text_observed
        ):
            self._first_text_observed.add(event.response_id)
            if self._measurement is not None:
                self._measurement.record_response_event(
                    response_id=event.response_id,
                    name="first_text_delta",
                    stage="llm",
                )
        if event.type == "response_audio_segment":
            if (
                event.audio is None
                or event.response_id is None
                or event.audio_sequence is None
            ):
                raise ValueError("audio event requires response metadata and PCM bytes")
            response_id = event.response_id
            first_audio = response_id not in self._first_audio_observed
            if first_audio and self._measurement is not None:
                self._measurement.record_response_event(
                    response_id=response_id,
                    name="tts_completed",
                    stage="tts",
                )
                self._measurement.record_response_event(
                    response_id=response_id,
                    name="first_audio_generated",
                    stage="tts",
                )
            await self._coordinator.send_logical_audio_segment(
                response_id=event.response_id,
                # private playback evidenceは0始まり、Core contractは1始まり。
                audio_sequence=event.audio_sequence - 1,
                pcm_sample_count=len(event.audio)
                // (PCM_SAMPLE_WIDTH_BYTES * PCM_CHANNELS),
            )
            await self._coordinator.send_core(self._voice_payload(event))
            first_capture_ns = await self._audio_source.publish(event.audio, response_id=response_id)
            await self._observe_first_audio_out(response_id, first_capture_ns)
            return
        if (
            event.type == "response_cancelled"
            and event.response_id is not None
            and self._measurement is not None
        ):
            if event.terminal_state_bounds_ns is not None:
                for name, timestamp in zip(("cancel_state_lower", "cancel_state_upper"),
                                           event.terminal_state_bounds_ns, strict=True):
                    self._measurement.record_response_event(
                        response_id=event.response_id, name=name, stage="response", timestamp=timestamp,
                    )
            self._measurement.record_response_event(
                response_id=event.response_id,
                name="response_excluded",
                stage="response",
                outcome="excluded",
                reason_code=event.reason or "response_cancelled",
            )
            self._measurement.record_response_event(
                response_id=event.response_id, name="response_cancelled", stage="response",
                outcome="excluded", reason_code=event.reason or "response_cancelled",
            )
        if (
            event.type == "response_privacy_skipped"
            and event.response_id is not None
            and self._measurement is not None
        ):
            self._measurement.record_response_event(
                response_id=event.response_id,
                name="response_excluded",
                stage="response",
                outcome="excluded",
                reason_code="privacy_skip",
            )
        if event.type == "response_completed" and event.response_id is not None:
            if event.response_id not in self._completed_output_responses:
                await self._finish_audio(event.response_id)
        if event.type in {"response_cancelled", "response_failed", "response_privacy_skipped"}:
            self._audio_source.clear(event.response_id)
        if event.type == "response_privacy_skipped":
            if event.source_utterance_ids is None:
                raise ValueError("privacy event requires source utterance ids")
            for utterance_id in event.source_utterance_ids:
                await self._coordinator.send_core(
                    self._voice_payload(event, utterance_id=utterance_id)
                )
        await self._coordinator.send_core(self._voice_payload(event))

    async def stop_response(self, response: Response) -> ResponseStopResult:
        if not isinstance(self._audio_source, ResponseAudioTracks):
            raise RuntimeError("output stop confirmation requires response audio tracks")
        if self._measurement is not None:
            self._measurement.record_response_event(
                response_id=response.response_id, name="output_stop_requested", stage="transport",
            )
        stopped, prefix = await asyncio.gather(
            self._audio_source.stop_response(response.response_id),
            self._coordinator.request_output_stop(response.response_id),
            return_exceptions=True,
        )
        if isinstance(stopped, BaseException) or not isinstance(prefix, ConfirmedOutputStop):
            raise RuntimeError("response output stop was not confirmed")
        if self._measurement is not None:
            self._measurement.record_response_event(
                response_id=response.response_id, name="output_stop_confirmed", stage="transport",
                # この確認eventのIDを要求nonceと揃え、privateな一次記録同士だけで照合する。
                event_id=prefix.request_id,
            )
        return ResponseStopResult(prefix.last_played_audio_sequence)

    async def finish_response(self, response: Response) -> None:
        if not response.audio_segments:
            self._completed_output_responses.add(response.response_id)
            return
        if isinstance(self._audio_source, ResponseAudioTracks):
            await self._completion_gate.wait(
                response.response_id, len(response.audio_segments),
                lambda: self._finish_audio(response.response_id),
            )
        else:
            await self._finish_audio(response.response_id)
        self._completed_output_responses.add(response.response_id)

    def confirm_response_playback(self, response_id: str, last_audio_sequence: int) -> bool:
        return self._completion_gate.confirm(response_id, last_audio_sequence)

    async def _finish_audio(self, response_id: str) -> None:
        first_capture_ns = await self._audio_source.finish_response(response_id)
        if isinstance(self._audio_source, ResponseAudioTracks):
            statistics = self._audio_source.statistics(response_id)
            if self._measurement is not None:
                for name, value in statistics.items():
                    self._measurement.record_response_event(
                        response_id=response_id, name=name, stage="transport", value=value,
                    )
            await self._coordinator.send_response_audio_finished(
                response_id=response_id,
                input_sample_count=statistics["response_audio_input_samples"],
                captured_sample_count=statistics["response_audio_captured_samples"],
                padding_sample_count=statistics["response_audio_padding_samples"],
            )
        await self._observe_first_audio_out(response_id, first_capture_ns)

    async def _observe_first_audio_out(self, response_id: str | None, timestamp_ns: int | None) -> None:
        if timestamp_ns is None or response_id is None or response_id in self._first_audio_observed:
            return
        self._first_audio_observed.add(response_id)
        if self._measurement is not None:
            self._measurement.record_response_event(
                response_id=response_id, name="first_audio_out", stage="transport", timestamp=timestamp_ns,
            )
        await self._coordinator.send_core(json.dumps({
            "type": "observation", "protocol_version": "2.0", "event_id": str(uuid4()),
            "session_id": self._session_id, "response_id": response_id,
            "measurement": "first_audio_out", "timestamp": str(timestamp_ns),
            "clock_domain": "server_monotonic", "unit": "nanosecond",
        }, separators=(",", ":")).encode())

    def _voice_payload(
        self, event: CoreEvent, *, utterance_id: str | None = None
    ) -> bytes:
        payload: dict[str, object] = {
            "protocol_version": "2.0",
            "event_id": str(uuid4()),
            "session_id": event.session_id,
            "monotonic_timestamp_ms": int(time.monotonic() * 1000),
        }
        if event.type == "response_started":
            if event.source_utterance_ids is None:
                raise ValueError("response_started requires source utterance ids")
            payload.update(
                type=event.type,
                response_id=event.response_id,
                speaker=self._character_speaker,
                source_utterance_ids=list(event.source_utterance_ids),
                source_inputs=[
                    {"input_id": item.input_id, "source": item.source}
                    for item in event.source_inputs
                ] if event.source_inputs is not None else [
                    {"input_id": item, "source": "speech"}
                    for item in event.source_utterance_ids
                ],
            )
            if event.history_turn_id is not None:
                payload["history_turn_id"] = event.history_turn_id
        elif event.type == "utterance_finalized":
            if (
                event.utterance_id is None
                or event.transcript is None
                or event.should_response is None
                or self._user_speaker is None
            ):
                raise ValueError(
                    "utterance_finalized requires user speaker and transcript"
                )
            payload.update(
                type=event.type,
                utterance_id=event.utterance_id,
                speaker=self._user_speaker,
                transcript=event.transcript,
                should_response=event.should_response,
            )
        elif event.type == "turn_decision":
            if (
                event.utterance_id is None
                or event.response_id is None
                or event.decision not in {"backchannel", "take_turn", "indeterminate"}
                or event.final is None
            ):
                raise ValueError("turn_decision requires decision metadata")
            payload.update(
                type=event.type,
                utterance_id=event.utterance_id,
                response_id=event.response_id,
                decision=event.decision,
                final=event.final,
            )
        elif event.type == "error":
            if (
                event.utterance_id is None
                or event.classification is None
                or event.error_code is None
                or event.user_state is None
            ):
                raise ValueError("error event requires utterance error metadata")
            payload.update(
                type=event.type,
                utterance_id=event.utterance_id,
                classification=event.classification,
                error_code=event.error_code,
                user_state=event.user_state,
            )
        elif event.type == "utterance_discarded":
            if event.utterance_id is None or event.reason is None:
                raise ValueError(
                    "utterance_discarded requires utterance id and reason"
                )
            payload.update(
                type=event.type,
                utterance_id=event.utterance_id,
                reason=event.reason,
            )
        elif event.type == "response_delta":
            if event.text_range is None:
                raise ValueError("response_delta requires text range")
            payload.update(
                type=event.type,
                response_id=event.response_id,
                text_sequence=event.text_sequence,
                text=event.text,
                text_range={
                    "start": event.text_range[0],
                    "end": event.text_range[1],
                },
            )
        elif event.type == "response_audio_segment":
            if event.text_range is None:
                raise ValueError("response_audio_segment requires text range")
            payload.update(
                type=event.type,
                response_id=event.response_id,
                audio_sequence=event.audio_sequence,
                text_range={
                    "start": event.text_range[0],
                    "end": event.text_range[1],
                },
            )
        elif event.type == "response_completed":
            payload.update(
                type=event.type,
                response_id=event.response_id,
                last_text_sequence=event.last_text_sequence,
                last_audio_sequence=event.last_audio_sequence,
            )
        elif event.type == "response_cancelled":
            payload.update(type=event.type, response_id=event.response_id, reason=event.reason)
        elif event.type == "response_failed":
            error_code = (
                event.reason
                if event.reason is not None
                else "conversation_core_failed"
            )
            payload.update(
                type=event.type,
                response_id=event.response_id,
                error_code=error_code,
                recoverable=True,
            )
        elif event.type == "response_privacy_skipped":
            if utterance_id is not None:
                # 既存の音声入力破棄通知を維持する。
                payload.update(type="utterance_discarded", utterance_id=utterance_id, reason="privacy")
            else:
                sources = event.source_inputs
                if not sources:
                    raise ValueError("privacy event requires source inputs")
                payload.update(
                    type=event.type, response_id=event.response_id,
                    source_inputs=[{"input_id": item.input_id, "source": item.source} for item in sources],
                )
        else:
            raise ValueError(f"unsupported Core event type: {event.type}")
        return json.dumps(payload, separators=(",", ":")).encode()
