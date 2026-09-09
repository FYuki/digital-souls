from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
import traceback
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TypeVar

from app.conversation_core.models import (
    AudioSegment,
    CoreEvent,
    Response,
    ResponseState,
    ResponseStopResult,
    StageObservation,
    TerminalOutcome,
    Utterance,
    UtteranceState,
)
from app.conversation_core.ports import (
    DeliveryPort,
    ResponseCancellationPort,
    ResponseCompletionPort,
    LlmPort,
    ObservationPort,
    PersistencePort,
    SttPort,
    TtsPort,
)
from app.conversation_core.provider_result_audit import ProviderResultAudit
from app.conversation_core.segmentation import JapaneseTextSegmenter, TextSegment
from app.conversation_core.turn_decision import TurnDecision, classify_turn
from app.inference.diagnostics import collect_diagnostics


TaskResult = TypeVar("TaskResult")
logger = logging.getLogger(__name__)


def _log_unhandled_task_error(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    _log_core_error(error)


def _log_core_error(error: BaseException) -> None:
    # 例外本文・ローカル変数・ユーザー入力を出さず、失敗箇所だけを残す。
    frames = [
        f"{Path(frame.f_code.co_filename).name}:{line}:{frame.f_code.co_name}"
        for frame, line in traceback.walk_tb(error.__traceback__)
    ][-8:]
    logger.error(
        "Core task failed: type=%s errno=%s frames=%s",
        type(error).__name__, error.errno if isinstance(error, OSError) else None,
        ";".join(frames),
    )


class TerminalProtocolError(RuntimeError):
    """同じ識別子または sequence が異なる payload を指している。"""


class DeliveryError(RuntimeError):
    """delivery port が Core event の送信に失敗した。"""


class ConversationCoreSession:
    def __init__(
        self,
        *,
        session_id: str,
        response_id_factory: Callable[[], str],
        delivery: DeliveryPort,
        persistence: PersistencePort,
        observation: ObservationPort,
        stt: SttPort,
        llm: LlmPort,
        tts: TtsPort,
        completion: ResponseCompletionPort | None = None,
        cancellation: ResponseCancellationPort | None = None,
        cancellation_timeout: float = 1.0,
        tts_queue_maxsize: int = 8,
        turn_classifier: Callable[[str], TurnDecision] = classify_turn,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if tts_queue_maxsize < 1:
            raise ValueError("tts_queue_maxsize must be positive")
        if not 0 < cancellation_timeout < float("inf"):
            raise ValueError("cancellation_timeout must be finite and positive")
        self.session_id = session_id
        self._response_id_factory = response_id_factory
        self._delivery = delivery
        self._persistence = persistence
        self._observation = observation
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._completion = completion
        self._cancellation = cancellation
        self._cancellation_timeout = cancellation_timeout
        self._tts_queue_maxsize = tts_queue_maxsize
        self._turn_classifier = turn_classifier
        self._monotonic_ns = monotonic_ns
        self._responses: dict[str, Response] = {}
        self._utterances: dict[str, Utterance] = {}
        self._active_response_id: str | None = None
        self._next_generation = 1
        self._connected = True
        self._ended = False
        self._state_lock = asyncio.Lock()
        self._stage_tasks: set[asyncio.Task[object]] = set()
        self._stage_task_keys: dict[asyncio.Task[object], tuple[str, int]] = {}
        self._effect_tasks: set[asyncio.Task[object]] = set()
        self._response_start_events: dict[str, asyncio.Event] = {}
        self._persisted_response_ids: set[str] = set()
        self._cancellation_tasks: dict[str, asyncio.Task[Response]] = {}
        self._output_stop_tasks: dict[str, asyncio.Task[ResponseStopResult]] = {}
        self._cancellation_outcomes: dict[str, tuple[ResponseState, str | None]] = {}
        self._closed_provider_audits: dict[str, ProviderResultAudit] = {}
        self._published_provider_audits: set[str] = set()
        self._event_payloads: dict[str, tuple[object, ...]] = {}
        self._text_payloads: dict[tuple[str, int], tuple[object, ...]] = {}
        self._audio_payloads: dict[tuple[str, int], tuple[object, ...]] = {}

    @property
    def active_response(self) -> Response | None:
        if self._active_response_id is None:
            return None
        return self._responses[self._active_response_id]

    @property
    def pending_utterances(self) -> tuple[Utterance, ...]:
        return tuple(
            utterance
            for utterance in self._utterances.values()
            if utterance.state is UtteranceState.PENDING
        )

    @property
    def running_stage_count(self) -> int:
        return sum(
            not task.done() for task in self._stage_tasks | self._effect_tasks
        )

    @property
    def accepting_input(self) -> bool:
        """新しい発話を受理できる接続状態かを返す。"""
        return self._connected and not self._ended

    async def prepare_transcription(self) -> bool:
        """任意の事前準備。接続終了後には新しい準備を投入しない。"""
        prepare = getattr(self._stt, "prepare", None)
        if not self.accepting_input or not callable(prepare):
            return False
        return bool(await prepare())

    def response(self, response_id: str) -> Response:
        return self._responses[response_id]

    def utterance(self, utterance_id: str) -> Utterance:
        return self._utterances[utterance_id]

    async def finalize_utterance(
        self,
        *,
        utterance_id: str,
        transcript: str,
        should_response: bool,
        retain_pending: bool = True,
    ) -> Response | None:
        start_task: asyncio.Task[Response] | None = None
        async with self._state_lock:
            self._require_available()
            existing = self._utterances.get(utterance_id)
            if existing is not None:
                if (existing.transcript, existing.should_response) != (
                    transcript,
                    should_response,
                ):
                    raise TerminalProtocolError(
                        "utterance_id is associated with a different payload"
                    )
                return self._response_containing(utterance_id)

            self._utterances[utterance_id] = Utterance(
                utterance_id=utterance_id,
                transcript=transcript,
                should_response=should_response,
                state=(
                    UtteranceState.PENDING
                    if retain_pending
                    else UtteranceState.CONSUMED
                ),
            )
            if self.active_response is None and should_response:
                response, response_input = self._reserve_pending_response_locked()
                start_task = self._register_effect_task(
                    self._start_reserved_response(response, response_input)
                )
        if start_task is None:
            return None
        return await start_task

    async def discard_utterance(self, *, utterance_id: str, reason: str) -> bool:
        """未処理の発話を理由付きで終端し、黙って欠落させない。"""
        async with self._state_lock:
            existing = self._utterances.get(utterance_id)
            if existing is not None:
                return False
            self._utterances[utterance_id] = Utterance(
                utterance_id=utterance_id,
                transcript="",
                should_response=False,
                state=UtteranceState.DISCARDED,
                discard_reason=reason,
            )
        await self._publish_utterance_delivery(
            CoreEvent(
                type="utterance_discarded",
                session_id=self.session_id,
                utterance_id=utterance_id,
                reason=reason,
            )
        )
        return True

    def start_transcription(
        self,
        *,
        utterance_id: str,
        audio: bytes,
        should_response: bool,
        interrupted_response_id: str | None = None,
    ) -> asyncio.Task[Response | None]:
        self._require_available()
        return self._register_stage_task(
            self._transcribe_utterance(
                utterance_id=utterance_id,
                audio=audio,
                should_response=should_response,
                interrupted_response_id=interrupted_response_id,
            )
        )

    async def preview_turn(
        self,
        *,
        utterance_id: str,
        audio: bytes,
        interrupted_response_id: str,
    ) -> TurnDecision:
        """発話冒頭の認識結果から暫定判定し、明確な発話なら旧回答を早期終了する。"""
        await self._record_utterance_stage(utterance_id, "stt_preview", "started")
        transcript = await self._stt.transcribe(audio)
        await self._record_utterance_stage(utterance_id, "stt_preview", "completed")
        decision = self._turn_classifier(transcript)
        await self._record_utterance_stage(
            utterance_id, "turn_decision", "completed"
        )
        if decision == "take_turn":
            await self._record_utterance_stage(
                utterance_id, "take_turn_decision", "completed"
            )
        await self._publish_utterance_delivery(
            CoreEvent(
                type="turn_decision",
                session_id=self.session_id,
                utterance_id=utterance_id,
                response_id=interrupted_response_id,
                decision=decision,
                final=False,
            )
        )
        if decision == "take_turn":
            cancelled = await self.cancel_response(
                response_id=interrupted_response_id,
                reason="barge_in",
            )
            if cancelled is not None and cancelled.state is ResponseState.CANCELLED:
                await self._record_utterance_stage(
                    utterance_id, "server_cancelled", "completed"
                )
        return decision

    async def accept_text_delta(
        self,
        *,
        response_id: str,
        generation: int,
        text_sequence: int,
        text: str,
        text_range: tuple[int, int],
        event_id: str | None = None,
    ) -> bool:
        event: CoreEvent
        async with self._state_lock:
            response = self._gated_response(response_id, generation)
            if response is None:
                return False
            payload = (response_id, generation, text_sequence, text, text_range)
            if event_id is not None and self._is_duplicate_event(event_id, payload):
                return False
            sequence_key = (response_id, text_sequence)
            existing = self._text_payloads.get(sequence_key)
            if existing is not None:
                if existing != payload:
                    raise TerminalProtocolError(
                        "text_sequence is associated with a different payload"
                    )
                return False
            expected_sequence = response.last_text_sequence + 1
            if text_sequence != expected_sequence:
                return False
            expected_range = (
                len(response.generated_text),
                len(response.generated_text) + len(text),
            )
            if text_range != expected_range:
                raise TerminalProtocolError("text_range is not contiguous")

            self._text_payloads[sequence_key] = payload
            if event_id is not None:
                self._event_payloads[event_id] = payload
            self._responses[response_id] = replace(
                response,
                generated_text=response.generated_text + text,
                last_text_sequence=text_sequence,
            )
            event = CoreEvent(
                type="response_delta",
                session_id=self.session_id,
                response_id=response_id,
                generation=generation,
                text_sequence=text_sequence,
                text=text,
                text_range=text_range,
            )
        await self._deliver_response_event(event, response_id, generation)
        return True

    async def accept_audio_segment(
        self,
        *,
        response_id: str,
        generation: int,
        audio_sequence: int,
        audio: bytes,
        text_range: tuple[int, int],
        event_id: str | None = None,
    ) -> bool:
        event: CoreEvent
        async with self._state_lock:
            response = self._gated_response(response_id, generation)
            if response is None:
                return False
            payload = (response_id, generation, audio_sequence, audio, text_range)
            if event_id is not None and self._is_duplicate_event(event_id, payload):
                return False
            sequence_key = (response_id, audio_sequence)
            existing = self._audio_payloads.get(sequence_key)
            if existing is not None:
                if existing != payload:
                    raise TerminalProtocolError(
                        "audio_sequence is associated with a different payload"
                    )
                return False
            if audio_sequence != len(response.audio_segments) + 1:
                return False
            start, end = text_range
            if start < 0 or end < start or end > len(response.generated_text):
                raise TerminalProtocolError("audio text_range is outside generated text")
            expected_start = (
                response.audio_segments[-1].text_range[1]
                if response.audio_segments
                else 0
            )
            if start != expected_start:
                skipped_text = response.generated_text[expected_start:start]
                if start < expected_start or not skipped_text.isspace():
                    raise TerminalProtocolError("audio text_range is not contiguous")
                # VOICEVOXへ空白だけの区間は渡さない。一方、再生済みprefixは
                # LLM本文上で連続させる必要があるため、直前の空白を次の音声へ含める。
                text_range = (expected_start, end)

            segment = AudioSegment(audio_sequence, audio, text_range)
            self._audio_payloads[sequence_key] = payload
            if event_id is not None:
                self._event_payloads[event_id] = payload
            self._responses[response_id] = replace(
                response,
                audio_segments=(*response.audio_segments, segment),
            )
            event = CoreEvent(
                type="response_audio_segment",
                session_id=self.session_id,
                response_id=response_id,
                generation=generation,
                audio_sequence=audio_sequence,
                audio=audio,
                text_range=text_range,
            )
        await self._deliver_response_event(event, response_id, generation)
        return True

    async def complete_response(self, *, response_id: str, generation: int) -> Response:
        return await self._terminate(
            response_id=response_id,
            generation=generation,
            state=ResponseState.COMPLETED,
            reason=None,
        )

    async def fail_response(
        self,
        *,
        response_id: str,
        generation: int,
        reason: str | None = None,
    ) -> Response:
        return await self._terminate(
            response_id=response_id,
            generation=generation,
            state=ResponseState.FAILED,
            reason=reason,
        )

    async def privacy_skip_response(
        self,
        *,
        response_id: str,
        generation: int,
        reason: str | None = "privacy",
    ) -> Response:
        return await self._terminate(
            response_id=response_id,
            generation=generation,
            state=ResponseState.PRIVACY_SKIPPED,
            reason=reason,
        )

    async def cancel_response(
        self, *, response_id: str, reason: str
    ) -> Response | None:
        if self._cancellation is not None:
            async with self._state_lock:
                response = self._responses.get(response_id)
                if response is None or response.state.is_terminal:
                    return response
                task = self._cancellation_tasks.get(response_id)
                if task is None:
                    response = replace(response, state=ResponseState.CANCELLING)
                    self._responses[response_id] = response
                    # 受付を閉じてから一度だけ停止を要求し、activeは確認まで保持する。
                    stop_task = asyncio.create_task(asyncio.wait_for(
                        self._cancellation.stop_response(response),
                        timeout=self._cancellation_timeout,
                    ))
                    self._output_stop_tasks[response_id] = stop_task
                    task = self._register_effect_task(
                        self._finish_cancellation(response, reason, stop_task)
                    )
                    self._cancellation_tasks[response_id] = task
            # 同時要求や呼出元の取消で、共通の停止処理を中断しない。
            return await asyncio.shield(task)
        response = self._responses.get(response_id)
        if response is None:
            return None
        result = await self._terminate(
            response_id=response_id,
            generation=response.generation,
            state=ResponseState.CANCELLED,
            reason=reason,
        )
        self._request_response_task_cancellation(response_id, response.generation)
        for _ in range(3):
            await asyncio.sleep(0)
        return result

    async def _finish_cancellation(
        self, response: Response, reason: str,
        stop_task: asyncio.Task[ResponseStopResult],
    ) -> Response:
        response_id = response.response_id
        stages = tuple(
            task for task, key in self._stage_task_keys.items()
            if key == (response_id, response.generation)
        )
        # 出力停止とprovider/deliveryの終了を並行して待つ。失敗しても終了待ちは省かない。
        _, stopped = await asyncio.gather(
            self._cancel_tasks(stages), stop_task, return_exceptions=True,
        )
        outcome = self._cancellation_outcomes.pop(response_id, None)
        if outcome is None:
            if isinstance(stopped, ResponseStopResult) and (
                type(stopped.last_played_audio_sequence) is int
                and 0 <= stopped.last_played_audio_sequence <= len(response.audio_segments)
            ):
                await self.confirm_playback(
                    response_id=response_id,
                    last_played_audio_sequence=stopped.last_played_audio_sequence,
                )
                outcome = (ResponseState.CANCELLED, reason)
            else:
                outcome = (ResponseState.FAILED, "output_stop_unconfirmed")
        result = await self._terminate(
            response_id=response_id, generation=response.generation,
            state=outcome[0], reason=outcome[1],
        )
        await self._publish_closed_provider_audit(response_id)
        self._output_stop_tasks.pop(response_id, None)
        return result

    async def confirm_playback(
        self, *, response_id: str, last_played_audio_sequence: int
    ) -> bool:
        async with self._state_lock:
            response = self._responses.get(response_id)
            if response is None:
                return False
            if response.state.is_terminal:
                return False
            if last_played_audio_sequence <= response.last_played_audio_sequence:
                return False
            if last_played_audio_sequence > len(response.audio_segments):
                raise TerminalProtocolError("playback sequence exceeds generated audio")
            self._responses[response_id] = replace(
                response,
                last_played_audio_sequence=last_played_audio_sequence,
            )
            return True

    def start_stage(
        self,
        *,
        response_id: str,
        generation: int,
        stage: str,
        operation: Coroutine[object, object, None],
    ) -> asyncio.Task[None]:
        if self._gated_response(response_id, generation) is None:
            operation.close()
            raise ValueError("stage does not belong to the active response")
        return self._register_stage_task(
            self._run_stage(response_id, generation, stage, operation),
            response_key=(response_id, generation),
        )

    async def stage_started(
        self, *, response_id: str, generation: int, stage: str
    ) -> None:
        await self._record_stage(response_id, generation, stage, "started")

    async def stage_completed(
        self, *, response_id: str, generation: int, stage: str
    ) -> None:
        await self._record_stage(response_id, generation, stage, "completed")

    async def stage_failed(
        self, *, response_id: str, generation: int, stage: str
    ) -> None:
        await self._record_stage(response_id, generation, stage, "failed")

    async def stage_cancelled(
        self, *, response_id: str, generation: int, stage: str
    ) -> None:
        await self._record_stage(response_id, generation, stage, "cancelled")

    async def disconnect(self) -> None:
        if self._ended or not self._connected:
            return
        self._connected = False
        await self._terminate_active_for_shutdown("disconnect")
        self._discard_pending("disconnect")
        await self._cancel_all_stage_tasks()
        await self._finish_effect_tasks()

    async def reconnect(self) -> None:
        if self._ended:
            raise RuntimeError("ended session cannot reconnect")
        self._connected = True

    async def end(self) -> None:
        if self._ended:
            return
        self._connected = False
        self._ended = True
        await self._terminate_active_for_shutdown("session_ended")
        self._discard_pending("session_ended")
        await self._cancel_all_stage_tasks()
        await self._finish_effect_tasks()

    def _reserve_pending_response_locked(self) -> tuple[Response, str]:
        pending = self.pending_utterances
        if not pending or self.active_response is not None:
            raise RuntimeError("response cannot start without pending input")
        response_id = self._response_id_factory()
        if response_id in self._responses:
            raise TerminalProtocolError("response_id must be unique within a session")
        generation = self._next_generation
        source_ids = tuple(item.utterance_id for item in pending)
        response_input = "\n".join(item.transcript for item in pending)
        self._next_generation += 1
        for utterance in pending:
            self._utterances[utterance.utterance_id] = replace(
                utterance,
                state=UtteranceState.CONSUMED,
            )
        response = Response(
            response_id=response_id,
            generation=generation,
            source_utterance_ids=source_ids,
            state=ResponseState.IN_PROGRESS,
        )
        self._responses[response_id] = response
        self._active_response_id = response_id
        self._response_start_events[response_id] = asyncio.Event()
        return response, response_input

    async def _start_reserved_response(
        self, response: Response, response_input: str
    ) -> Response:
        try:
            start_result = await self._persistence.start_response(
                response_id=response.response_id,
                user_content=response_input,
            )
        except DeliveryError:
            self._response_start_events[response.response_id].set()
            await self.stage_cancelled(
                response_id=response.response_id,
                generation=response.generation,
                stage="llm",
            )
            raise
        except Exception as error:
            _log_core_error(error)
            # terminal effectを起動する前にstart待ちを解除し、開始失敗時の
            # 相互待機を作らない。
            self._response_start_events[response.response_id].set()
            await self.fail_response(
                response_id=response.response_id,
                generation=response.generation,
            )
            return self._responses[response.response_id]
        self._response_start_events[response.response_id].set()
        if start_result.content_skipped:
            await self.privacy_skip_response(
                response_id=response.response_id,
                generation=response.generation,
            )
            return self._responses[response.response_id]
        if self._gated_response(response.response_id, response.generation) is None:
            return response
        try:
            await self._deliver_response_event(
                CoreEvent(
                    type="response_started",
                    session_id=self.session_id,
                    response_id=response.response_id,
                    generation=response.generation,
                    source_utterance_ids=response.source_utterance_ids,
                    history_turn_id=start_result.history_turn_id,
                ),
                response.response_id,
                response.generation,
            )
        except DeliveryError:
            await self.fail_response(
                response_id=response.response_id,
                generation=response.generation,
            )
            return self._responses[response.response_id]
        # 開始通知の配送待ち中にもcancel・切断・終了は成立する。
        # その待機から戻った後で、終了済み応答のprovider処理を新規起動しない。
        if self._gated_response(response.response_id, response.generation) is None:
            return self._responses[response.response_id]
        self._register_stage_task(
            self._run_response_pipeline(response, response_input),
            response_key=(response.response_id, response.generation),
        )
        return response

    async def _transcribe_utterance(
        self,
        *,
        utterance_id: str,
        audio: bytes,
        should_response: bool,
        interrupted_response_id: str | None,
    ) -> Response | None:
        await self._record_utterance_stage(utterance_id, "stt", "started")
        try:
            transcript = await self._stt.transcribe(audio)
        except asyncio.CancelledError:
            await self._record_utterance_stage(utterance_id, "stt", "cancelled")
            raise
        except Exception as error:
            await self._record_utterance_stage(utterance_id, "stt", "failed")
            error_code = getattr(error, "error_code", None)
            if isinstance(error_code, str):
                await self._discard_failed_utterance(
                    utterance_id=utterance_id,
                    should_response=should_response,
                    reason=error_code,
                )
                await self._publish_utterance_delivery(
                    CoreEvent(
                        type="error",
                        session_id=self.session_id,
                        utterance_id=utterance_id,
                        classification="recoverable",
                        error_code=error_code,
                        recoverable=True,
                        user_state="listening",
                    )
                )
            raise
        await self._record_utterance_stage(utterance_id, "stt", "completed")
        retain_pending = True
        if interrupted_response_id is not None:
            decision = self._turn_classifier(transcript)
            should_response = decision == "take_turn"
            retain_pending = should_response
            await self._record_utterance_stage(
                utterance_id, "turn_decision", "completed"
            )
            if should_response:
                await self._record_utterance_stage(
                    utterance_id, "take_turn_decision", "completed"
                )
            await self._publish_utterance_delivery(
                CoreEvent(
                    type="turn_decision",
                    session_id=self.session_id,
                    utterance_id=utterance_id,
                    response_id=interrupted_response_id,
                    decision=decision,
                    final=True,
                )
            )
            if should_response:
                cancelled = await self.cancel_response(
                    response_id=interrupted_response_id,
                    reason="barge_in",
                )
                if cancelled is not None and cancelled.state is ResponseState.CANCELLED:
                    await self._record_utterance_stage(
                        utterance_id, "server_cancelled", "completed"
                    )
        await self._publish_utterance_delivery(
            CoreEvent(
                type="utterance_finalized",
                session_id=self.session_id,
                utterance_id=utterance_id,
                transcript=transcript,
                should_response=should_response,
            )
        )
        return await self.finalize_utterance(
            utterance_id=utterance_id,
            transcript=transcript,
            should_response=should_response,
            retain_pending=retain_pending,
        )

    async def _discard_failed_utterance(
        self, *, utterance_id: str, should_response: bool, reason: str
    ) -> None:
        async with self._state_lock:
            existing = self._utterances.get(utterance_id)
            if existing is not None:
                return
            self._utterances[utterance_id] = Utterance(
                utterance_id=utterance_id,
                transcript="",
                should_response=should_response,
                state=UtteranceState.DISCARDED,
                discard_reason=reason,
            )

    async def _run_response_pipeline(
        self,
        response: Response,
        response_input: str,
    ) -> None:
        audit = ProviderResultAudit()
        try:
            await self._run_observed_response_pipeline(response, response_input, audit)
        finally:
            # innerは取消時も両consumerの終了を待つ。受付が続く間にゼロでcloseしない。
            self._closed_provider_audits[response.response_id] = audit
            await self._publish_closed_provider_audit(response.response_id)

    async def _publish_closed_provider_audit(self, response_id: str) -> None:
        response = self._responses[response_id]
        audit = self._closed_provider_audits.get(response_id)
        if audit is None or response_id in self._published_provider_audits:
            return
        if not response.state.is_terminal:
            return
        self._published_provider_audits.add(response_id)
        self._closed_provider_audits.pop(response_id, None)
        statistics = audit.stopping_statistics()
        if response.state is ResponseState.CANCELLED:
            statistics.update(audit.closed_statistics())
        for name, value in statistics.items():
            await self._observation.record(StageObservation(
                session_id=self.session_id, response_id=response_id,
                generation=response.generation, stage=name, outcome="completed", value=value,
            ))

    async def _run_observed_response_pipeline(
        self, response: Response, response_input: str, audit: ProviderResultAudit,
    ) -> None:
        queue: asyncio.Queue[TextSegment | None] = asyncio.Queue(
            maxsize=self._tts_queue_maxsize
        )
        llm_task = asyncio.create_task(
            self._produce_text_segments(response, response_input, queue, audit)
        )
        tts_task = asyncio.create_task(self._consume_text_segments(response, queue, audit))
        try:
            await asyncio.gather(llm_task, tts_task)
            current = self._gated_response(response.response_id, response.generation)
            if current is not None and self._completion is not None:
                await self._completion.finish_response(current)
        except asyncio.CancelledError:
            # gatherから伝播済みの取消を重ね、providerのfinallyを中断しない。
            for task in (llm_task, tts_task):
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(llm_task, tts_task, return_exceptions=True)
            raise
        except Exception as error:
            # gatherから伝播済みの取消を重ね、providerのfinallyを中断しない。
            for task in (llm_task, tts_task):
                if not task.cancelling():
                    task.cancel()
            results = await asyncio.gather(llm_task, tts_task, return_exceptions=True)
            logger.warning(
                "Conversation response pipeline failed: session_id=%s response_id=%s generation=%d error_type=%s llm_outcome=%s tts_outcome=%s",
                self.session_id,
                response.response_id,
                response.generation,
                type(error).__name__,
                self._pipeline_task_outcome(results[0]),
                self._pipeline_task_outcome(results[1]),
            )
            if self._gated_response(response.response_id, response.generation) is not None:
                await self.fail_response(
                    response_id=response.response_id,
                    generation=response.generation,
                    reason="streaming_pipeline_failed",
                )
            return
        if self._gated_response(response.response_id, response.generation) is None:
            return
        await self.complete_response(
            response_id=response.response_id,
            generation=response.generation,
        )

    @staticmethod
    def _pipeline_task_outcome(result: object) -> str:
        if isinstance(result, BaseException):
            return type(result).__name__
        return "completed"

    async def _produce_text_segments(
        self,
        response: Response,
        response_input: str,
        queue: asyncio.Queue[TextSegment | None],
        audit: ProviderResultAudit,
    ) -> None:
        await self.stage_started(
            response_id=response.response_id,
            generation=response.generation,
            stage="llm",
        )
        segmenter = JapaneseTextSegmenter()
        try:
            async with self._measure_llm_stage(response):
                async for delta in self._llm.generate(response_input):
                    state = self._responses[response.response_id].state
                    audit.text(
                        delta.text, cancelled=state is ResponseState.CANCELLED,
                        stopping=state is ResponseState.CANCELLING,
                    )
                    accepted = await self.accept_text_delta(
                        response_id=response.response_id,
                        generation=response.generation,
                        text_sequence=delta.text_sequence,
                        text=delta.text,
                        text_range=delta.text_range,
                    )
                    if not accepted or self._gated_response(response.response_id, response.generation) is None:
                        continue
                    for segment in segmenter.feed(delta.text):
                        await queue.put(segment)
            if self._gated_response(response.response_id, response.generation) is not None:
                for segment in segmenter.finish():
                    await queue.put(segment)
                await queue.put(None)
        except asyncio.CancelledError:
            await self.stage_cancelled(
                response_id=response.response_id,
                generation=response.generation,
                stage="llm",
            )
            raise
        except DeliveryError:
            await self.stage_cancelled(
                response_id=response.response_id,
                generation=response.generation,
                stage="llm",
            )
            raise
        except Exception:
            await self.stage_failed(
                response_id=response.response_id,
                generation=response.generation,
                stage="llm",
            )
            raise
        if self._gated_response(response.response_id, response.generation) is None:
            await self.stage_cancelled(
                response_id=response.response_id,
                generation=response.generation,
                stage="llm",
            )
            return
        await self.stage_completed(
            response_id=response.response_id,
            generation=response.generation,
            stage="llm",
        )

    @asynccontextmanager
    async def _measure_llm_stage(self, response: Response) -> AsyncIterator[None]:
        with collect_diagnostics() as diagnostics:
            try:
                yield
            finally:
                for event in diagnostics.finish():
                    await self._observation.record(StageObservation(
                        session_id=self.session_id,
                        response_id=response.response_id,
                        generation=response.generation,
                        stage=event.name,
                        outcome="completed",
                        timestamp_ns=event.timestamp_ns,
                        value=event.value,
                    ))

    async def _consume_text_segments(
        self,
        response: Response,
        queue: asyncio.Queue[TextSegment | None],
        audit: ProviderResultAudit,
    ) -> None:
        stage_started = False
        audio_sequence = 0
        try:
            while self._gated_response(response.response_id, response.generation) is not None:
                text_segment = await queue.get()
                try:
                    if text_segment is None:
                        break
                    if not stage_started:
                        # queue待機はTTS処理時間ではない。最初の合成可能segmentを
                        # 受け取った時点を#17のTTS開始点にする。
                        await self.stage_started(
                            response_id=response.response_id,
                            generation=response.generation,
                            stage="tts",
                        )
                        stage_started = True
                    if self._gated_response(response.response_id, response.generation) is None:
                        break
                    async for synthesized in self._tts.synthesize(text_segment.text):
                        state = self._responses[response.response_id].state
                        audit.audio(
                            synthesized.audio, cancelled=state is ResponseState.CANCELLED,
                            stopping=state is ResponseState.CANCELLING,
                        )
                        audio_sequence += 1
                        local_start, local_end = synthesized.text_range
                        global_range = (
                            text_segment.text_range[0] + local_start,
                            text_segment.text_range[0] + local_end,
                        )
                        await self.accept_audio_segment(
                            response_id=response.response_id,
                            generation=response.generation,
                            audio_sequence=audio_sequence,
                            audio=synthesized.audio,
                            text_range=global_range,
                        )
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            if stage_started:
                await self.stage_cancelled(
                    response_id=response.response_id,
                    generation=response.generation,
                    stage="tts",
                )
            raise
        except DeliveryError:
            if stage_started:
                await self.stage_cancelled(
                    response_id=response.response_id,
                    generation=response.generation,
                    stage="tts",
                )
            raise
        except Exception:
            if stage_started:
                await self.stage_failed(
                    response_id=response.response_id,
                    generation=response.generation,
                    stage="tts",
                )
            raise
        if not stage_started:
            return
        if self._gated_response(response.response_id, response.generation) is None:
            await self.stage_cancelled(
                response_id=response.response_id,
                generation=response.generation,
                stage="tts",
            )
            return
        await self.stage_completed(
            response_id=response.response_id,
            generation=response.generation,
            stage="tts",
        )

    async def _run_llm_stage(self, response: Response, response_input: str) -> bool:
        await self.stage_started(
            response_id=response.response_id,
            generation=response.generation,
            stage="llm",
        )
        try:
            async with self._measure_llm_stage(response):
                async for delta in self._llm.generate(response_input):
                    await self.accept_text_delta(
                        response_id=response.response_id,
                        generation=response.generation,
                        text_sequence=delta.text_sequence,
                        text=delta.text,
                        text_range=delta.text_range,
                    )
        except asyncio.CancelledError:
            await self.stage_cancelled(
                response_id=response.response_id,
                generation=response.generation,
                stage="llm",
            )
            raise
        except DeliveryError:
            await self.stage_cancelled(
                response_id=response.response_id,
                generation=response.generation,
                stage="llm",
            )
            await self.fail_response(
                response_id=response.response_id,
                generation=response.generation,
            )
            return False
        except Exception:
            await self.stage_failed(
                response_id=response.response_id,
                generation=response.generation,
                stage="llm",
            )
            await self.fail_response(
                response_id=response.response_id,
                generation=response.generation,
            )
            return False
        return await self._record_stage_return_outcome(
            response_id=response.response_id,
            generation=response.generation,
            stage="llm",
        )

    async def _run_tts_stage(self, response: Response) -> bool:
        await self.stage_started(
            response_id=response.response_id,
            generation=response.generation,
            stage="tts",
        )
        try:
            async for segment in self._tts.synthesize(response.generated_text):
                await self.accept_audio_segment(
                    response_id=response.response_id,
                    generation=response.generation,
                    audio_sequence=segment.audio_sequence,
                    audio=segment.audio,
                    text_range=segment.text_range,
                )
        except asyncio.CancelledError:
            await self.stage_cancelled(
                response_id=response.response_id,
                generation=response.generation,
                stage="tts",
            )
            raise
        except DeliveryError:
            await self.stage_cancelled(
                response_id=response.response_id,
                generation=response.generation,
                stage="tts",
            )
            await self.fail_response(
                response_id=response.response_id,
                generation=response.generation,
            )
            return False
        except Exception:
            await self.stage_failed(
                response_id=response.response_id,
                generation=response.generation,
                stage="tts",
            )
            await self.fail_response(
                response_id=response.response_id,
                generation=response.generation,
            )
            return False
        return await self._record_stage_return_outcome(
            response_id=response.response_id,
            generation=response.generation,
            stage="tts",
        )

    async def _terminate(
        self,
        *,
        response_id: str,
        generation: int,
        state: ResponseState,
        reason: str | None,
    ) -> Response:
        terminal_started = False
        async with self._state_lock:
            response = self._responses[response_id]
            if response.state.is_terminal:
                return response
            if response.generation != generation:
                return response
            if (
                response.state is ResponseState.CANCELLING
                and asyncio.current_task() is not self._cancellation_tasks.get(response_id)
            ):
                # 完了競合は無視する。失敗・privacyは停止待ちの後で終端させる。
                if state in {ResponseState.FAILED, ResponseState.PRIVACY_SKIPPED}:
                    previous = self._cancellation_outcomes.get(response_id)
                    if previous is None or state is ResponseState.PRIVACY_SKIPPED:
                        self._cancellation_outcomes[response_id] = (state, reason)
                    self._output_stop_tasks[response_id].cancel()
                return response
            response = replace(response, state=state, terminal_reason=reason)
            # awaitを挟まず、公開状態の書き換えを同じ単調時計の2点で囲む。
            # 通知の送信・永続化完了をcancel成立時刻へ読み替えない。
            transition_before = self._monotonic_ns() if state is ResponseState.CANCELLED else None
            self._responses[response_id] = response
            transition_after = self._monotonic_ns() if state is ResponseState.CANCELLED else None
            if self._active_response_id == response_id:
                self._active_response_id = None
            outcome = TerminalOutcome(
                response_id=response.response_id,
                generation=response.generation,
                state=response.state,
                reason=response.terminal_reason,
                generated_text=response.generated_text,
                audio_segments=response.audio_segments,
                last_played_audio_sequence=response.last_played_audio_sequence,
                last_text_sequence=response.last_text_sequence,
                source_utterance_ids=response.source_utterance_ids,
                terminal_state_bounds_ns=(transition_before, transition_after)
                if transition_before is not None and transition_after is not None else None,
            )
            if response_id not in self._persisted_response_ids:
                self._persisted_response_ids.add(response_id)
                self._register_effect_task(self._run_terminal_effects(outcome))
                terminal_started = True
        if terminal_started:
            await asyncio.sleep(0)
        return response

    async def _run_terminal_effects(self, outcome: TerminalOutcome) -> None:
        start_event = self._response_start_events[outcome.response_id]
        await start_event.wait()
        try:
            try:
                await self._persistence.persist(outcome)
            finally:
                # 両方が失敗した場合はdelivery失敗を主例外とし、永続化失敗を
                # exception contextへ残す。
                await self._publish_delivery(
                    CoreEvent(
                        type=self._terminal_event_type(outcome.state),
                        session_id=self.session_id,
                        response_id=outcome.response_id,
                        generation=outcome.generation,
                        reason=outcome.reason,
                        source_utterance_ids=outcome.source_utterance_ids,
                        last_text_sequence=outcome.last_text_sequence,
                        last_audio_sequence=len(outcome.audio_segments),
                        terminal_state_bounds_ns=outcome.terminal_state_bounds_ns,
                    )
                )
        finally:
            await self._start_pending_after_terminal()

    async def _start_pending_after_terminal(self) -> None:
        async with self._state_lock:
            if (
                not self._connected
                or self.active_response is not None
                or not any(
                    utterance.should_response for utterance in self.pending_utterances
                )
            ):
                return
            response, response_input = self._reserve_pending_response_locked()
        await self._start_reserved_response(response, response_input)

    async def _terminate_active_for_shutdown(self, reason: str) -> None:
        response = self.active_response
        if response is None:
            return
        if response.state is ResponseState.CANCELLING:
            await self.fail_response(
                response_id=response.response_id, generation=response.generation,
                reason=f"output_stop_unconfirmed_{reason}",
            )
            await asyncio.shield(self._cancellation_tasks[response.response_id])
            return
        await self._terminate(
            response_id=response.response_id,
            generation=response.generation,
            state=ResponseState.CANCELLED,
            reason=reason,
        )

    def _discard_pending(self, reason: str) -> None:
        for utterance in self.pending_utterances:
            self._utterances[utterance.utterance_id] = replace(
                utterance,
                state=UtteranceState.DISCARDED,
                discard_reason=reason,
            )

    async def _run_stage(
        self,
        response_id: str,
        generation: int,
        stage: str,
        operation: Coroutine[object, object, None],
    ) -> None:
        await self.stage_started(
            response_id=response_id,
            generation=generation,
            stage=stage,
        )
        try:
            await operation
        except asyncio.CancelledError:
            await self.stage_cancelled(
                response_id=response_id,
                generation=generation,
                stage=stage,
            )
            raise
        except Exception:
            await self.stage_failed(
                response_id=response_id,
                generation=generation,
                stage=stage,
            )
            await self.fail_response(
                response_id=response_id,
                generation=generation,
            )
            raise
        await self._record_stage_return_outcome(
            response_id=response_id,
            generation=generation,
            stage=stage,
        )

    async def _record_stage_return_outcome(
        self,
        *,
        response_id: str,
        generation: int,
        stage: str,
    ) -> bool:
        response = self._responses[response_id]
        if response.generation != generation:
            raise TerminalProtocolError(
                "stage generation does not match its response"
            )
        if response.state in {ResponseState.CANCELLING, ResponseState.CANCELLED}:
            await self.stage_cancelled(
                response_id=response_id,
                generation=generation,
                stage=stage,
            )
            return False
        await self.stage_completed(
            response_id=response_id,
            generation=generation,
            stage=stage,
        )
        return True

    async def _record_stage(
        self,
        response_id: str,
        generation: int,
        stage: str,
        outcome: str,
    ) -> None:
        await self._observation.record(
            StageObservation(
                session_id=self.session_id,
                response_id=response_id,
                generation=generation,
                stage=stage,
                outcome=outcome,
            )
        )

    async def _record_utterance_stage(
        self,
        utterance_id: str,
        stage: str,
        outcome: str,
    ) -> None:
        await self._observation.record(
            StageObservation(
                session_id=self.session_id,
                response_id=None,
                generation=None,
                utterance_id=utterance_id,
                stage=stage,
                outcome=outcome,
            )
        )

    async def _deliver_response_event(
        self, event: CoreEvent, response_id: str, generation: int
    ) -> None:
        task = self._register_stage_task(
            self._publish_delivery(event),
            response_key=(response_id, generation),
        )
        await task

    async def _publish_delivery(self, event: CoreEvent) -> None:
        response_id = event.response_id
        generation = event.generation
        if response_id is None or generation is None:
            raise TerminalProtocolError(
                "response delivery event requires response identity"
            )
        await self._record_stage(
            response_id,
            generation,
            "delivery",
            "started",
        )
        try:
            await self._delivery.publish(event)
        except asyncio.CancelledError:
            await self._record_stage(
                response_id,
                generation,
                "delivery",
                "cancelled",
            )
            raise
        except Exception as error:
            _log_core_error(error)
            await self._record_stage(
                response_id,
                generation,
                "delivery",
                "failed",
            )
            raise DeliveryError("Core event delivery failed") from error
        await self._record_stage(
            response_id,
            generation,
            "delivery",
            "completed",
        )

    async def _publish_utterance_delivery(self, event: CoreEvent) -> None:
        utterance_id = event.utterance_id
        if utterance_id is None:
            raise TerminalProtocolError(
                "utterance delivery event requires utterance identity"
            )
        await self._record_utterance_stage(utterance_id, "delivery", "started")
        try:
            await self._delivery.publish(event)
        except asyncio.CancelledError:
            await self._record_utterance_stage(
                utterance_id, "delivery", "cancelled"
            )
            raise
        except Exception as error:
            await self._record_utterance_stage(utterance_id, "delivery", "failed")
            raise DeliveryError("Core utterance delivery failed") from error
        await self._record_utterance_stage(utterance_id, "delivery", "completed")

    def _register_stage_task(
        self,
        operation: Coroutine[object, object, TaskResult],
        *,
        response_key: tuple[str, int] | None = None,
    ) -> asyncio.Task[TaskResult]:
        task = asyncio.create_task(operation)
        self._stage_tasks.add(task)
        if response_key is not None:
            self._stage_task_keys[task] = response_key
        task.add_done_callback(self._forget_stage_task)
        return task

    def _register_effect_task(
        self,
        operation: Coroutine[object, object, TaskResult],
    ) -> asyncio.Task[TaskResult]:
        task = asyncio.create_task(operation)
        self._effect_tasks.add(task)
        task.add_done_callback(self._forget_effect_task)
        return task

    def _request_response_task_cancellation(
        self, response_id: str, generation: int
    ) -> None:
        current = asyncio.current_task()
        for task, key in tuple(self._stage_task_keys.items()):
            if key == (response_id, generation) and task is not current:
                task.cancel()

    async def _cancel_all_stage_tasks(self) -> None:
        await self._cancel_tasks(tuple(self._stage_tasks))

    async def _finish_effect_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(task for task in self._effect_tasks if task is not current)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_tasks(self, tasks: tuple[asyncio.Task[object], ...]) -> None:
        current = asyncio.current_task()
        cancellable = tuple(task for task in tasks if task is not current)
        for task in cancellable:
            task.cancel()
        if cancellable:
            await asyncio.gather(*cancellable, return_exceptions=True)

    def _forget_stage_task(self, task: asyncio.Task[object]) -> None:
        self._stage_tasks.discard(task)
        self._stage_task_keys.pop(task, None)
        _log_unhandled_task_error(task)

    def _forget_effect_task(self, task: asyncio.Task[object]) -> None:
        self._effect_tasks.discard(task)
        _log_unhandled_task_error(task)

    def _gated_response(self, response_id: str, generation: int) -> Response | None:
        response = self._responses.get(response_id)
        if (
            not self._connected
            or response is None
            or response.state is not ResponseState.IN_PROGRESS
            or response.generation != generation
            or self._active_response_id != response_id
        ):
            return None
        return response

    def _is_duplicate_event(
        self, event_id: str, payload: tuple[object, ...]
    ) -> bool:
        existing = self._event_payloads.get(event_id)
        if existing is None:
            return False
        if existing != payload:
            raise TerminalProtocolError(
                "event_id is associated with a different payload"
            )
        return True

    def _response_containing(self, utterance_id: str) -> Response | None:
        return next(
            (
                response
                for response in self._responses.values()
                if utterance_id in response.source_utterance_ids
            ),
            None,
        )

    def _require_available(self) -> None:
        if not self._connected or self._ended:
            raise RuntimeError("session is not available")

    @staticmethod
    def _terminal_event_type(state: ResponseState) -> str:
        return {
            ResponseState.COMPLETED: "response_completed",
            ResponseState.CANCELLED: "response_cancelled",
            ResponseState.FAILED: "response_failed",
            ResponseState.PRIVACY_SKIPPED: "response_privacy_skipped",
        }[state]
