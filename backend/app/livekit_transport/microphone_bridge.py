"""microphone入力、VAD、STT bridgeを所有する。"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from app.conversation_core import ConversationCoreSession
from app.conversation_core.models import ResponseState
from app.livekit_transport.bootstrap import (
    BOOTSTRAP_TIMEOUT_SECONDS,
    preparation_operation,
)
from app.livekit_transport.delivery import TerminalProtocolError
from app.livekit_transport.measurement import LiveKitMeasurementSession
from app.livekit_transport.microphone_integrity import AudioIntegrityFault
from app.livekit_transport.production_sdk import PCM_SAMPLE_WIDTH_BYTES
from app.livekit_transport.stt_audio import (
    PcmCaptureSpan, SttSignalSpan, prepare_stt_audio, stt_preparation_statistics,
)
from app.livekit_transport.text_input import TextInputReceiver
from app.voice_input.models import check_ready
from app.voice_input.pipeline import AudioInputFault, VoiceInputPipeline
from app.voice_input.session import BackendVoiceInput, InputGrant, SpeechBoundary
from app.voice_session_metrics import SessionMetrics

logger = logging.getLogger(__name__)

STT_SAMPLE_RATE = 16_000
STT_MAX_PENDING_UTTERANCES = 3
STT_MAX_OPEN_CAPTURES = STT_MAX_PENDING_UTTERANCES + 1
STT_MAX_UTTERANCE_PCM_BYTES = STT_SAMPLE_RATE * PCM_SAMPLE_WIDTH_BYTES * 30
STT_MAX_PENDING_PCM_BYTES = STT_MAX_PENDING_UTTERANCES * STT_MAX_UTTERANCE_PCM_BYTES
# Whisperはstreaming APIではないため、最初の静音閾値超過から800msのsnapshotを先行認識する。
# 発話前のpre-rollの長さをこの800msへ含めない。
STT_TURN_PREVIEW_PCM_BYTES = int(STT_SAMPLE_RATE * PCM_SAMPLE_WIDTH_BYTES * 0.8)
STT_TURN_PREVIEW_MAX_ATTEMPTS = 3
# 発話確認までの遅れを含め、通知前の語頭をmedia側で最大2秒保持する。
# #150の固定fixtureで確認通知が語頭から1,440ms遅れる条件があり、800msでは語頭を失う。
STT_MICROPHONE_PREROLL_BYTES = STT_SAMPLE_RATE * PCM_SAMPLE_WIDTH_BYTES * 2
# 確認済み発話で300msの静音を受けたら、VAD終了待ちとSTT準備を重ねる。
# 入力の切断・削除・発話確定には使わない。
STT_PREPARATION_QUIET_SAMPLES = STT_SAMPLE_RATE * 300 // 1000


@dataclass
class _UserAudioCapture:
    utterance_id: str
    interrupted_response_id: str | None = None
    pcm: bytearray = field(default_factory=bytearray)
    finalized: bool = False
    integrity_verified: bool = False
    capacity_exceeded: bool = False
    preview_attempts: int = 0
    preview_complete: bool = False
    preview_last_signal_samples: int = 0
    preview_signal: SttSignalSpan = field(default_factory=SttSignalSpan)
    received_span: PcmCaptureSpan = field(default_factory=PcmCaptureSpan)
    media_offset_samples: int | None = None
    vad_started_sample: int | None = None
    vad_active_end_sample: int | None = None
    vad_detected_end_sample: int | None = None
    media_end_anchor_valid: bool = False
    preparation_started: bool = False
    quiet_samples: int = 0


class _ConversationCoreBridge:
    def __init__(
        self,
        session: ConversationCoreSession,
        schedule: Callable[[Awaitable[None]], None],
        stop_audio: Callable[[str], None] = lambda _response_id: None,
        confirm_response_playback: Callable[[str, int], bool] = lambda _response_id, _sequence: False,
        measurement: LiveKitMeasurementSession | None = None,
        session_metrics: SessionMetrics | None = None,
        text_input: TextInputReceiver | None = None,
        publish_audio_event: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        authorize_microphone: Callable[[str], Awaitable[bool]] | None = None,
        verify_audio_integrity: Callable[[str, int, int], Awaitable[None]] | None = None,
        user_participant_id: str | None = None,
    ) -> None:
        self._session = session
        self._text_input = text_input
        self._voice_input: BackendVoiceInput | None = None
        self._publish_audio_event = publish_audio_event
        self._authorize_microphone = authorize_microphone
        self._verify_audio_integrity = verify_audio_integrity
        self._user_participant_id = user_participant_id
        self._playback_response_id: str | None = None
        self._schedule = schedule
        self._stop_audio = stop_audio
        self._confirm_response_playback = confirm_response_playback
        self._measurement = measurement
        self._session_metrics = session_metrics
        self._user_audio_captures: deque[_UserAudioCapture] = deque()
        self._pending_transcriptions: deque[tuple[str, bytes, str | None]] = deque()
        self._pending_transcription_bytes = 0
        self._transcription_active = False
        self._transcription_epoch = 0
        self._microphone_preroll = bytearray()
        self._microphone_received_bytes = 0
        self._control_lock = asyncio.Lock()
        self._text_focus_suppressed = False
        self._manual_input_muted = False

    def notify(self, payload: bytes) -> None:
        event = json.loads(payload)
        if event["type"] in {"speech_started", "speech_stopped"}:
            raise TerminalProtocolError("speech boundaries are owned by Backend")
        if event["type"] == "audio_input_open_requested":
            self._schedule(self._open_audio(event))
            return
        if event["type"] in {"audio_input_suppression_changed", "session_muted", "session_resumed", "user_text_submitted"}:
            if self._voice_input is None:
                raise TerminalProtocolError("audio input is not ready")
            reason = "text_priority" if event["type"] == "user_text_submitted" else "input_suppressed"
            if not self._voice_input.suppress(reason=reason, input_revision=event["input_revision"]):
                if event["type"] != "user_text_submitted":
                    return
                self._voice_input.suppress(reason="text_priority")
        if event["type"] in {"session_disconnected", "session_reconnected"} and self._voice_input is not None:
            self._voice_input.suppress(reason="disconnect")
            self._microphone_preroll.clear()
        if event["type"] == "audio_input_suppression_changed":
            self._text_focus_suppressed = bool(event["suppressed"])
            self._apply_audio_input_gate()
            return
        if event["type"] in {"session_muted", "session_resumed"}:
            self._manual_input_muted = event["type"] == "session_muted"
            self._apply_audio_input_gate()
            return
        if event["type"] in {"user_text_submitted", "user_input_result_requested"}:
            if self._text_input is None:
                raise TerminalProtocolError("text input receiver is not connected")
            # 受付が非同期処理中でも照合要求はprocessingの結果へ到達できる。
            self._schedule(self._text_input.receive(event))
            return
        if event["type"] == "observation":
            if self._measurement is not None:
                self._measurement.record_client_observation(event)
            return
        self._schedule(self._receive_serialized(event))

    async def prepare_audio(self) -> None:
        def prepare() -> VoiceInputPipeline:
            check_ready()
            return VoiceInputPipeline()
        task = asyncio.create_task(asyncio.to_thread(prepare))
        try:
            async with preparation_operation("vad", BOOTSTRAP_TIMEOUT_SECONDS):
                pipeline = await asyncio.shield(task)
        except BaseException:
            def release(done: asyncio.Task[VoiceInputPipeline]) -> None:
                if not done.cancelled() and done.exception() is None:
                    done.result().close()
            task.add_done_callback(release)
            raise
        self._voice_input = BackendVoiceInput(
            pipeline, on_frame=lambda frame: self.receive_microphone(frame.pcm),
            on_started=self._audio_started, on_stopped=self._audio_stopped,
            on_discarded=self._audio_discarded,
        )

    def _audio_event(self, kind: str, **fields: object) -> dict[str, object]:
        return {"type": kind, "protocol_version": "2.0", "event_id": str(uuid4()),
                "session_id": self._session.session_id,
                "monotonic_timestamp_ms": time.monotonic_ns() // 1_000_000, **fields}

    async def _publish_audio(self, event: dict[str, object]) -> None:
        if self._publish_audio_event is None:
            raise RuntimeError("audio event delivery is not connected")
        await self._publish_audio_event(event)

    def _speech_event(self, kind: str, boundary: SpeechBoundary) -> dict[str, object]:
        detection = boundary.detection
        return self._audio_event(
            kind, utterance_id=boundary.utterance_id,
            speaker={"role": "user", "participant_id": self._user_participant_id},
            input_generation=boundary.grant.input_generation, track_sid=boundary.grant.track_sid,
            start_sample=detection.started_sample, detected_sample=detection.detected_sample,
            active_end_sample=detection.active_end_sample, sample_rate=STT_SAMPLE_RATE,
            clock_domain="server_monotonic",
        )

    def _audio_started(self, boundary: SpeechBoundary) -> None:
        event = self._speech_event("speech_started", boundary)
        active = self._session.active_response
        response_id = active.response_id if active is not None else self._playback_response_id
        if response_id is not None:
            try:
                response = self._session.response(response_id)
            except KeyError:
                response_id = None
            else:
                if response.state not in {ResponseState.IN_PROGRESS, ResponseState.COMPLETED}:
                    response_id = None
        if response_id is not None:
            event["response_id"] = response_id
        self._begin_capture(event)
        self._schedule(self._publish_audio(event))

    async def _audio_stopped(self, boundary: SpeechBoundary) -> None:
        capture = next((item for item in self._user_audio_captures
                        if item.utterance_id == boundary.utterance_id), None)
        if capture is None:
            return
        # 発話終了位置は既にPCMで確認済み。focus抑止はこの区間を取り消さない。
        capture.finalized = True
        # await前に、この終了frameまでの受信連番とVADのtrack sample位置を固定する。
        detection = boundary.detection
        capture.vad_active_end_sample = detection.active_end_sample
        capture.vad_detected_end_sample = detection.detected_sample
        capture.media_end_anchor_valid = (
            capture.media_offset_samples is not None
            and capture.vad_started_sample == detection.started_sample
            and self._microphone_received_bytes // 2 + capture.media_offset_samples
            == detection.detected_sample
        )
        try:
            await self._publish_audio(self._speech_event("speech_stopped", boundary))
            if self._measurement is not None:
                self._measurement.record_utterance_event(
                    utterance_id=boundary.utterance_id, name="vad_speech_end", stage="vad",
                )
            if self._verify_audio_integrity is None:
                raise AudioInputFault("audio_integrity_unavailable")
            await self._verify_audio_integrity(
                boundary.grant.track_sid, boundary.detection.started_sample, boundary.detection.detected_sample,
            )
        except AudioInputFault as error:
            if self._measurement is not None and isinstance(error, AudioIntegrityFault):
                for name, value in error.statistics.items():
                    self._measurement.record_utterance_event(
                        utterance_id=boundary.utterance_id, name=name, stage="input_integrity",
                        value=value,
                    )
            self._audio_discarded(boundary.utterance_id, error.code)
            # 先行発話が失敗しても、別世代で終了・検証済みの後続を滞留させない。
            await self._finalize_user_audio_if_ready()
            return
        except BaseException:
            # track交換や切断はreaderの確定待ちも取り消す。未検証の先頭captureを
            # 残すと、別trackで検証済みの後続発話までSTTへ進めなくなる。
            # text優先で既に破棄済みなら重ねて通知しない。取消・元例外は伝播する。
            if any(item is capture for item in self._user_audio_captures):
                self._audio_discarded(boundary.utterance_id, "audio_integrity_unavailable")
                self._schedule(self._finalize_user_audio_if_ready())
            raise
        if not any(item is capture for item in self._user_audio_captures):
            return
        # BE自身がPCM境界を検出するため、FE通知後のmedia tail待機は不要。
        capture.integrity_verified = True
        await self._finalize_user_audio_if_ready()

    def _audio_discarded(self, utterance_id: str | None, reason: str) -> None:
        # 終了済み発話の検証失敗は、新trackで蓄積中のprerollを所有しない。
        if utterance_id is None or any(
            item.utterance_id == utterance_id and not item.finalized
            for item in self._user_audio_captures
        ):
            self._microphone_preroll.clear()
        if utterance_id is not None:
            self._user_audio_captures = deque(
                item for item in self._user_audio_captures if item.utterance_id != utterance_id
            )
            normalized = {
                "invalid_audio_frame": "invalid_audio",
                "vad_backlog_exceeded": "input_capacity_exceeded",
                "vad_reset_required": "vad_unavailable",
                "vad_processing_failed": "vad_unavailable",
                "microphone_track_unavailable": "audio_gap",
            }.get(reason, reason)
            self._schedule(self._discard_capture(utterance_id, normalized))
        if reason not in {"input_suppressed", "input_replaced", "input_closed", "text_priority", "disconnect", "microphone_track_unavailable"}:
            self._schedule(self._publish_audio(self._audio_event(
                "error", classification="recoverable", error_code="audio_input_repeat_required",
                user_state="listening",
            )))

    async def _open_audio(self, event: dict[str, object]) -> None:
        request_id, track_sid = str(event["event_id"]), str(event["track_sid"])
        try:
            source = self._voice_input
            if source is None:
                raise AudioInputFault("vad_unavailable")
            if self._audio_input_suppressed:
                raise AudioInputFault("input_suppressed")
            if self._authorize_microphone is None or not await self._authorize_microphone(track_sid):
                raise AudioInputFault("microphone_track_unavailable")
            if self._audio_input_suppressed:
                raise AudioInputFault("input_suppressed")
            if self._voice_input is not source:
                raise AudioInputFault("vad_unavailable")
            grant = await source.open(
                track_sid=track_sid, request_id=request_id, input_revision=int(str(event["input_revision"])),
            )
            self._microphone_preroll.clear()
            await self._publish_audio(self._audio_event(
                "audio_input_opened", request_event_id=request_id, track_sid=grant.track_sid,
                input_generation=grant.input_generation, input_revision=grant.input_revision,
            ))
        except AudioInputFault as error:
            await self._publish_audio(self._audio_event(
                "audio_input_rejected", request_event_id=request_id, reason=error.code,
            ))

    async def receive_microphone_frame(self, pcm: bytes, *, start_sample: int, track_sid: str) -> None:
        source = self._voice_input
        if source is None or self._audio_input_suppressed:
            return
        grant = source.grant
        if grant is None or grant.track_sid != track_sid:
            return
        try:
            await source.receive(pcm, start_sample=start_sample, grant=grant)
        except AudioInputFault as error:
            # resetの失敗は当該入力だけを停止する。旧世代の失敗で新入力を止めない。
            if self._voice_input is source and source.revision == grant.input_revision:
                logger.warning("LiveKit microphone input reset failed: reason=%s", error.code)
                self._audio_unavailable(grant)

    def _audio_unavailable(self, grant: InputGrant) -> None:
        self._schedule(self._publish_audio(self._audio_event(
            "error", classification="recoverable", error_code="audio_input_unavailable",
            user_state="muted", track_sid=grant.track_sid,
            input_generation=grant.input_generation, input_revision=grant.input_revision,
        )))

    def close_microphone_track(
        self, track_sid: str, *, reason: str = "microphone_track_unavailable",
        reader_reason: str | None = None,
    ) -> None:
        source = self._voice_input
        if source is not None and source.grant is not None and source.grant.track_sid == track_sid:
            grant = source.grant
            source.suppress(reason=reason)
            self._microphone_preroll.clear()
            if reason == "microphone_track_unavailable":
                # 無音中のreader終了にも通知し、聴取中表示を残さない。
                logger.warning("LiveKit authorized microphone reader ended: reason=%s", reader_reason or reason)
                self._audio_unavailable(grant)

    async def close_audio(self) -> None:
        source, self._voice_input = self._voice_input, None
        if source is not None:
            await source.close()

    def _begin_capture(self, event: dict[str, object]) -> None:
        if self._audio_input_suppressed:
            return
        utterance_id = str(event["utterance_id"])
        if self._measurement is not None:
            interrupted_response_id = event.get("response_id")
            if isinstance(interrupted_response_id, str):
                self._measurement.bind_interruption(
                    utterance_id=utterance_id, response_id=interrupted_response_id,
                )
            self._measurement.record_utterance_event(
                utterance_id=utterance_id,
                name="speech_started",
                stage="vad",
            )
        open_captures = sum(
            not capture.finalized for capture in self._user_audio_captures
        )
        if open_captures >= STT_MAX_OPEN_CAPTURES:
            oldest_open = next(
                capture
                for capture in self._user_audio_captures
                if not capture.finalized
            )
            self._user_audio_captures.remove(oldest_open)
            self._schedule(
                self._discard_capture(oldest_open.utterance_id)
            )
        capture = _UserAudioCapture(
            utterance_id=utterance_id,
            interrupted_response_id=(
                str(event["response_id"])
                if isinstance(event.get("response_id"), str)
                else None
            ),
        )
        detected, started = event.get("detected_sample"), event.get("start_sample")
        if (event.get("type") == "speech_started"
                and type(detected) is int and type(started) is int
                and 0 <= started <= detected):
            # on_frameがこのconfirmed frameをcaptureへ渡した直後に呼ばれる。
            # 世代間でbridgeの受信連番は継続するため、offsetは負にもなり得る。
            capture.media_offset_samples = detected - self._microphone_received_bytes // 2
            capture.vad_started_sample = started
        capture.received_span.append(
            self._microphone_received_bytes - len(self._microphone_preroll),
            len(self._microphone_preroll),
        )
        capture.pcm.extend(self._microphone_preroll)
        self._microphone_preroll.clear()
        self._user_audio_captures.append(capture)
        self._consider_stt_preparation(capture, bytes(capture.pcm))
        return

    @property
    def _audio_input_suppressed(self) -> bool:
        return self._text_focus_suppressed or self._manual_input_muted

    def invalidate_unfinalized_audio(self) -> tuple[str, ...]:
        """新しいtext受付時に、未確定capture/待機STTを次の入力世代から切り離す。"""
        discarded = tuple(dict.fromkeys([
            *(capture.utterance_id for capture in self._user_audio_captures),
            *(utterance_id for utterance_id, _, _ in self._pending_transcriptions),
        ]))
        self._transcription_epoch += 1
        self._transcription_active = False
        self._user_audio_captures.clear()
        self._pending_transcriptions.clear()
        self._pending_transcription_bytes = 0
        self._microphone_preroll.clear()
        return discarded

    def _apply_audio_input_gate(self) -> None:
        if not self._audio_input_suppressed:
            return
        # mediaとcontrolの到着順は異なる。抑止中のframe/prerollを復帰後へ渡さない。
        self._microphone_preroll.clear()
        for capture in tuple(self._user_audio_captures):
            if not capture.finalized:
                self._user_audio_captures.remove(capture)
                self._schedule(self._discard_capture(
                    utterance_id=capture.utterance_id, reason="input_suppressed",
                ))

    async def _discard_capture(
        self, utterance_id: str, reason: str = "input_capacity_exceeded",
    ) -> None:
        await self._session.discard_utterance(
            utterance_id=utterance_id,
            reason=reason,
        )

    async def _receive_serialized(self, event: dict[str, object]) -> None:
        async with self._control_lock:
            await self._receive(event)

    def receive_microphone(self, pcm: bytes) -> None:
        received_start_byte = self._microphone_received_bytes
        self._microphone_received_bytes += len(pcm)
        if self._audio_input_suppressed:
            return
        capture = next(
            (item for item in reversed(self._user_audio_captures) if not item.finalized),
            None,
        )
        if capture is None:
            # BEがPCMから確定した終了境界を越える音声は次の入力のprerollにする。
            # 終了通知・欠落確認の待機中にも、確定済みcaptureへ追加しない。
            self._microphone_preroll.extend(pcm)
            excess = len(self._microphone_preroll) - STT_MICROPHONE_PREROLL_BYTES
            if excess > 0:
                del self._microphone_preroll[:excess]
            return
        if self._measurement is not None:
            self._measurement.record_utterance_event(
                utterance_id=capture.utterance_id,
                name="user_audio_received",
                stage="transport",
            )
        if len(capture.pcm) + len(pcm) > STT_MAX_UTTERANCE_PCM_BYTES:
            capture.capacity_exceeded = True
        elif not capture.capacity_exceeded:
            capture.received_span.append(received_start_byte, len(pcm))
            capture.pcm.extend(pcm)
        self._consider_stt_preparation(capture, pcm)
        self._consider_turn_preview(capture)

    def _consider_turn_preview(self, capture: _UserAudioCapture) -> None:
        if (capture.interrupted_response_id is None or capture.preview_complete
                or capture.preview_attempts >= STT_TURN_PREVIEW_MAX_ATTEMPTS
                or capture.finalized or capture.capacity_exceeded or self._transcription_active
                or not getattr(self._session, "accepting_input", True)
                or not any(item is capture for item in self._user_audio_captures)):
            return
        signal_samples = capture.preview_signal.sample_count(capture.pcm)
        if (signal_samples - capture.preview_last_signal_samples) * PCM_SAMPLE_WIDTH_BYTES < STT_TURN_PREVIEW_PCM_BYTES:
            return
        # 相槌で始まる長い発話は追加800msで再評価する。全体STTを優先し、最大3回に限定する。
        capture.preview_attempts += 1
        capture.preview_last_signal_samples = signal_samples
        self._transcription_active = True
        if self._measurement is not None:
            suffix = "" if capture.preview_attempts == 1 else f"_attempt_{capture.preview_attempts}"
            for name, value in {
                "stt_preview_raw_samples": len(capture.pcm) // PCM_SAMPLE_WIDTH_BYTES,
                "stt_preview_signal_span_samples": signal_samples,
            }.items():
                self._measurement.record_utterance_event(
                    utterance_id=capture.utterance_id, name=name + suffix, stage="stt_preview", value=value,
                )
        self._schedule(self._preview_user_turn(
            utterance_id=capture.utterance_id,
            interrupted_response_id=capture.interrupted_response_id,
            microphone_pcm=bytes(capture.pcm), capture=capture,
            epoch=self._transcription_epoch,
        ))

    def _consider_stt_preparation(self, capture: _UserAudioCapture, pcm: bytes) -> None:
        if (capture.preparation_started or capture.finalized or capture.capacity_exceeded
                or capture.interrupted_response_id is not None or self._transcription_active
                or not getattr(self._session, "accepting_input", True)
                or not callable(getattr(self._session, "prepare_transcription", None))):
            return
        if len(pcm) % PCM_SAMPLE_WIDTH_BYTES:
            capture.quiet_samples = 0
            return
        # 先頭trimと同じ静音閾値。静音が続かない環境では通常STTへ任せる。
        for (sample,) in struct.iter_unpack("<h", pcm):
            capture.quiet_samples = capture.quiet_samples + 1 if abs(sample) <= 16 else 0
        if capture.quiet_samples >= STT_PREPARATION_QUIET_SAMPLES:
            capture.preparation_started = True
            self._schedule(self._prepare_transcription(capture))

    async def _prepare_transcription(self, capture: _UserAudioCapture) -> None:
        # scheduleと実行の間に発話確定・切断された場合も、新規要求を増やさない。
        if (capture.finalized or capture.capacity_exceeded or self._transcription_active
                or not getattr(self._session, "accepting_input", True)
                or not any(item is capture for item in self._user_audio_captures)):
            return
        if self._measurement is not None:
            self._measurement.record_utterance_event(
                utterance_id=capture.utterance_id, name="stt_preparation_started",
                stage="stt_preparation",
            )
        completed = False
        try:
            completed = await self._session.prepare_transcription()
        except asyncio.CancelledError:
            if self._measurement is not None:
                self._measurement.record_utterance_event(
                    utterance_id=capture.utterance_id, name="stt_preparation_await_cancelled",
                    stage="stt_preparation",
                )
            raise
        except Exception:
            # 任意の最適化の失敗を通常認識の失敗件数へ混ぜない。
            pass
        if self._measurement is not None:
            self._measurement.record_utterance_event(
                utterance_id=capture.utterance_id, name="stt_preparation_finished",
                stage="stt_preparation", value=int(completed),
            )

    async def _preview_user_turn(
        self,
        *,
        utterance_id: str,
        interrupted_response_id: str,
        microphone_pcm: bytes,
        capture: _UserAudioCapture | None = None,
        epoch: int | None = None,
    ) -> None:
        if epoch is None:
            epoch = self._transcription_epoch
        attempt = capture.preview_attempts if capture is not None else 1
        try:
            if epoch != self._transcription_epoch or not getattr(self._session, "accepting_input", True):
                return
            if self._measurement is not None:
                self._measurement.record_utterance_event(
                    utterance_id=utterance_id, name=f"stt_preview_attempt_{attempt}_started", stage="stt_preview",
                )
            prepared_audio, removed_samples = prepare_stt_audio(microphone_pcm)
            if self._measurement is not None:
                for name, value in stt_preparation_statistics(microphone_pcm, prepared_audio, removed_samples).items():
                    self._measurement.record_utterance_event(
                        utterance_id=utterance_id, name=f'stt_preview_attempt_{attempt}_' + name.removeprefix('stt_'),
                        stage='stt_preview', value=value,
                    )
            decision = await self._session.preview_turn(
                utterance_id=utterance_id,
                audio=prepared_audio,
                interrupted_response_id=interrupted_response_id,
                input_is_current=lambda: epoch == self._transcription_epoch and not self._audio_input_suppressed and (
                    capture is None or any(item is capture for item in self._user_audio_captures)
                ),
            )
            if capture is not None:
                capture.preview_complete = decision == "take_turn"
            if self._measurement is not None:
                self._measurement.record_utterance_event(
                    utterance_id=utterance_id, name=f"stt_preview_attempt_{attempt}_completed", stage="stt_preview",
                )
        except asyncio.CancelledError:
            if capture is not None:
                capture.preview_complete = True
            raise
        except Exception:
            # 先行認識の失敗時も発話全体のSTTで確定できる。
            logger.exception("Turn preview failed: utterance_id=%s", utterance_id)
        finally:
            if epoch == self._transcription_epoch:
                self._transcription_active = False
                self._start_next_transcription()
                if capture is not None:
                    self._consider_turn_preview(capture)

    async def _finalize_user_audio_if_ready(self) -> None:
        while self._user_audio_captures:
            capture = self._user_audio_captures[0]
            if not capture.finalized:
                return
            if not capture.pcm:
                if not any(item.pcm for item in tuple(self._user_audio_captures)[1:]):
                    return
                self._user_audio_captures.popleft()
                continue
            # 各発話の欠落確認が成功するまでSTTへ渡さない。
            # 音声のない旧captureは従来どおり後続を妨げず取り除く。
            if not capture.integrity_verified:
                return
            utterance_id = capture.utterance_id
            microphone_pcm = bytes(capture.pcm)
            self._user_audio_captures.popleft()
            if capture.capacity_exceeded:
                await self._session.discard_utterance(
                    utterance_id=utterance_id,
                    reason="input_capacity_exceeded",
                )
                continue
            if self._measurement is not None:
                statistics = capture.received_span.statistics(microphone_pcm)
                valid_media = capture.media_end_anchor_valid and statistics["stt_capture_received_span_valid"] == 1
                statistics["stt_capture_media_span_valid"] = 0
                if valid_media and capture.media_offset_samples is not None:
                    media_start = statistics["stt_capture_received_start_sample"] + capture.media_offset_samples
                    media_end = statistics["stt_capture_received_end_sample"] + capture.media_offset_samples
                    if (0 <= media_start <= media_end and media_end == capture.vad_detected_end_sample
                            and capture.vad_started_sample is not None
                            and capture.vad_active_end_sample is not None):
                        statistics.update(
                            stt_capture_media_span_valid=1,
                            stt_capture_media_start_sample=media_start,
                            stt_capture_media_end_sample=media_end,
                            vad_started_sample=capture.vad_started_sample,
                            vad_active_end_sample=capture.vad_active_end_sample,
                            vad_detected_end_sample=media_end,
                        )
                for name, value in statistics.items():
                    self._measurement.record_utterance_event(
                        utterance_id=utterance_id, name=name, stage='stt_capture', value=value,
                    )
            await self._enqueue_user_audio(
                utterance_id=utterance_id,
                microphone_pcm=microphone_pcm,
                interrupted_response_id=capture.interrupted_response_id,
            )

    async def _receive(self, event: dict[str, object]) -> None:
        event_type = event["type"]
        if event_type == "playback_started":
            response_id = event.get("response_id")
            if isinstance(response_id, str):
                try:
                    response = self._session.response(response_id)
                except KeyError:
                    return
                if response.state in {ResponseState.IN_PROGRESS, ResponseState.COMPLETED}:
                    self._playback_response_id = response_id
        elif event_type == "response_cancel_requested":
            response_id = event.get("response_id")
            reason = event.get("reason")
            if not isinstance(response_id, str) or not isinstance(reason, str):
                self._log_invalid_control_event(event_type)
                return
            await self._session.cancel_response(
                response_id=response_id,
                reason=reason,
            )
        elif event_type in ("playback_completed", "playback_stopped"):
            response_id = event.get("response_id")
            last_played_audio_sequence = event.get("last_played_audio_sequence")
            if (
                not isinstance(response_id, str)
                or type(last_played_audio_sequence) is not int
            ):
                self._log_invalid_control_event(event_type)
                return
            if self._playback_response_id == response_id:
                self._playback_response_id = None
            if event_type == "playback_stopped":
                # prefix検証や永続化が失敗しても、旧音声をlocal graph再接続後へ残さない。
                self._stop_audio(response_id)
            await self._session.confirm_playback(
                response_id=response_id,
                last_played_audio_sequence=last_played_audio_sequence,
            )
            if event_type == "playback_completed" and event.get("response_finished") is True:
                accepted = self._confirm_response_playback(response_id, last_played_audio_sequence)
                if accepted and self._measurement is not None and "playback_summary" in event:
                    recorded = self._measurement.record_playback_summary(
                        response_id=response_id, summary=event["playback_summary"],
                    )
                    if recorded and self._session_metrics is not None:
                        self._session_metrics.completed_playback(response_id)
        elif event_type == "session_disconnected":
            await self._session.disconnect()
        elif event_type == "session_reconnected":
            await self._session.reconnect()

    async def _enqueue_user_audio(
        self,
        *,
        utterance_id: str,
        microphone_pcm: bytes,
        interrupted_response_id: str | None = None,
    ) -> None:
        if not getattr(self._session, "accepting_input", True):
            return
        if self._transcription_active:
            if (
                len(self._pending_transcriptions) >= STT_MAX_PENDING_UTTERANCES
                or self._pending_transcription_bytes + len(microphone_pcm)
                > STT_MAX_PENDING_PCM_BYTES
            ):
                await self._session.discard_utterance(
                    utterance_id=utterance_id,
                    reason="input_capacity_exceeded",
                )
                return
            self._pending_transcriptions.append(
                (utterance_id, microphone_pcm, interrupted_response_id)
            )
            self._pending_transcription_bytes += len(microphone_pcm)
            return
        self._start_user_transcription(
            utterance_id, microphone_pcm, interrupted_response_id
        )

    def _start_user_transcription(
        self,
        utterance_id: str,
        microphone_pcm: bytes,
        interrupted_response_id: str | None = None,
    ) -> None:
        original_pcm = microphone_pcm
        microphone_pcm, trimmed_samples = prepare_stt_audio(original_pcm)
        self._transcription_active = True
        if self._measurement is not None:
            # 本文・波形は残さず、STTへ渡したPCMの長さと振幅だけを確認する。
            samples = [value[0] for value in struct.iter_unpack("<h", microphone_pcm)]
            statistics = {
                **stt_preparation_statistics(original_pcm, microphone_pcm, trimmed_samples),
                "stt_input_sample_count": len(samples),
                "stt_preroll_trimmed_samples": trimmed_samples,
                "stt_input_peak_pcm16": max((abs(value) for value in samples), default=0),
                "stt_input_rms_pcm16": (sum(value * value for value in samples) / max(1, len(samples))) ** .5,
                "stt_input_active_samples": sum(abs(value) > 200 for value in samples),
                "stt_input_first_nonzero_sample": next((i for i, value in enumerate(samples) if value != 0), len(samples)),
                "stt_input_first_above_16_sample": next((i for i, value in enumerate(samples) if abs(value) > 16), len(samples)),
                "stt_input_first_active_sample": next((i for i, value in enumerate(samples) if abs(value) > 200), len(samples)),
                "stt_input_last_active_sample": next((len(samples) - 1 - i for i, value in enumerate(reversed(samples)) if abs(value) > 200), len(samples)),
            }
            for name, value in statistics.items():
                self._measurement.record_utterance_event(
                    utterance_id=utterance_id, name=name, stage="stt", value=value,
                )
        try:
            if interrupted_response_id is None:
                task = self._session.start_transcription(
                    utterance_id=utterance_id,
                    audio=microphone_pcm,
                    should_response=True,
                )
            else:
                task = self._session.start_transcription(
                    utterance_id=utterance_id,
                    audio=microphone_pcm,
                    should_response=True,
                    interrupted_response_id=interrupted_response_id,
                )
        except RuntimeError:
            # disconnect/endとmedia tail確定の競合では、旧発話を次世代へ持ち越さない。
            if not getattr(self._session, "accepting_input", True):
                self._transcription_active = False
                return
            raise
        epoch = self._transcription_epoch
        task.add_done_callback(lambda task: self._transcription_done(task, epoch))

    def _transcription_done(self, task: asyncio.Task[object], epoch: int) -> None:
        self._consume_task(task)
        if epoch != self._transcription_epoch:
            return
        self._transcription_active = False
        self._start_next_transcription()

    def _start_next_transcription(self) -> None:
        if self._transcription_active:
            return
        if not getattr(self._session, "accepting_input", True):
            self._pending_transcriptions.clear()
            self._pending_transcription_bytes = 0
            return
        if not self._pending_transcriptions:
            return
        utterance_id, microphone_pcm, interrupted_response_id = (
            self._pending_transcriptions.popleft()
        )
        self._pending_transcription_bytes -= len(microphone_pcm)
        self._start_user_transcription(
            utterance_id, microphone_pcm, interrupted_response_id
        )

    @staticmethod
    def _is_user_event(event: dict[str, object]) -> bool:
        speaker = event.get("speaker")
        return isinstance(speaker, dict) and speaker.get("role") == "user"

    async def end(self) -> None:
        await self.close_audio()
        await self._session.end()

    @staticmethod
    def _log_invalid_control_event(event_type: object) -> None:
        logger.warning(
            "Invalid Conversation Core control event discarded: type=%s",
            event_type,
        )

    @staticmethod
    def _consume_task(task: asyncio.Task[object]) -> None:
        if not task.cancelled():
            task.exception()
