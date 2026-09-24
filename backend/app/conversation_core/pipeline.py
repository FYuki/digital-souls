from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from app.conversation_core.models import Response, ResponseState
from app.conversation_core.ports import LlmPort, TtsPort
from app.conversation_core.provider_result_audit import ProviderResultAudit
from app.conversation_core.segmentation import JapaneseTextSegmenter, TextSegment
from app.conversation_core.errors import DeliveryError
from app.inference.diagnostics import DiagnosticEvent, collect_diagnostics

logger = logging.getLogger(__name__)

AcceptTextDelta = Callable[..., Awaitable[bool]]
AcceptAudioSegment = Callable[..., Awaitable[bool]]
RecordStage = Callable[[str, str], Awaitable[None]]
RecordDiagnostic = Callable[[DiagnosticEvent], Awaitable[None]]


async def run_response_pipeline(
    *,
    response: Response,
    response_input: str,
    llm: LlmPort,
    tts: TtsPort,
    tts_queue_maxsize: int,
    accept_text_delta: AcceptTextDelta,
    accept_audio_segment: AcceptAudioSegment,
    is_response_current: Callable[[], bool],
    complete_response: Callable[[Response], Awaitable[None]],
    fail_response: Callable[[str], Awaitable[None]] | None = None,
    record_stage: RecordStage | None = None,
    record_diagnostic: RecordDiagnostic | None = None,
    get_response_state: Callable[[], ResponseState] | None = None,
    audit: ProviderResultAudit | None = None,
) -> None:
    queue: asyncio.Queue[TextSegment | None] = asyncio.Queue(
        maxsize=tts_queue_maxsize
    )
    llm_task = asyncio.create_task(
        _produce_text_segments(
            response=response,
            response_input=response_input,
            queue=queue,
            llm=llm,
            accept_text_delta=accept_text_delta,
            is_response_current=is_response_current,
            record_stage=record_stage,
            record_diagnostic=record_diagnostic,
            get_response_state=get_response_state,
            audit=audit,
        )
    )
    tts_task = asyncio.create_task(
        _consume_text_segments(
            response=response,
            queue=queue,
            tts=tts,
            accept_audio_segment=accept_audio_segment,
            is_response_current=is_response_current,
            record_stage=record_stage,
            get_response_state=get_response_state,
            audit=audit,
        )
    )
    try:
        await asyncio.gather(llm_task, tts_task)
        if is_response_current():
            await complete_response(response)
    except asyncio.CancelledError:
        _cancel_provider_tasks(llm_task, tts_task)
        await asyncio.gather(llm_task, tts_task, return_exceptions=True)
        raise
    except Exception as error:
        _cancel_provider_tasks(llm_task, tts_task)
        results = await asyncio.gather(llm_task, tts_task, return_exceptions=True)
        logger.warning(
            "Conversation response pipeline failed: response_id=%s generation=%d "
            "error_type=%s llm_outcome=%s tts_outcome=%s",
            response.response_id,
            response.generation,
            type(error).__name__,
            _task_outcome(results[0]),
            _task_outcome(results[1]),
        )
        if fail_response is not None and is_response_current():
            await fail_response("streaming_pipeline_failed")
        return


def _cancel_provider_tasks(*tasks: asyncio.Task[object]) -> None:
    for task in tasks:
        if not task.cancelling():
            task.cancel()


def _task_outcome(result: object) -> str:
    if isinstance(result, BaseException):
        return type(result).__name__
    return "completed"


async def _record_stage(
    record_stage: RecordStage | None, stage: str, outcome: str
) -> None:
    if record_stage is not None:
        await record_stage(stage, outcome)


async def _produce_text_segments(
    *,
    response: Response,
    response_input: str,
    queue: asyncio.Queue[TextSegment | None],
    llm: LlmPort,
    accept_text_delta: AcceptTextDelta,
    is_response_current: Callable[[], bool],
    record_stage: RecordStage | None,
    record_diagnostic: RecordDiagnostic | None,
    get_response_state: Callable[[], ResponseState] | None,
    audit: ProviderResultAudit | None,
) -> None:
    await _record_stage(record_stage, "llm", "started")
    segmenter = JapaneseTextSegmenter()
    try:
        async with _measure_llm_stage(response, record_diagnostic):
            async for delta in llm.generate(response_input):
                state = get_response_state() if get_response_state else response.state
                if audit is not None:
                    audit.text(
                        delta.text,
                        cancelled=state is ResponseState.CANCELLED,
                        stopping=state is ResponseState.CANCELLING,
                    )
                accepted = await accept_text_delta(
                    response_id=response.response_id,
                    generation=response.generation,
                    text_sequence=delta.text_sequence,
                    text=delta.text,
                    text_range=delta.text_range,
                )
                if not accepted or not is_response_current():
                    continue
                for segment in segmenter.feed(delta.text):
                    await queue.put(segment)
        if is_response_current():
            for segment in segmenter.finish():
                await queue.put(segment)
            await queue.put(None)
    except asyncio.CancelledError:
        await _record_stage(record_stage, "llm", "cancelled")
        raise
    except DeliveryError:
        await _record_stage(record_stage, "llm", "cancelled")
        raise
    except Exception:
        await _record_stage(record_stage, "llm", "failed")
        raise
    if not is_response_current():
        await _record_stage(record_stage, "llm", "cancelled")
        return
    await _record_stage(record_stage, "llm", "completed")


async def _consume_text_segments(
    *,
    response: Response,
    queue: asyncio.Queue[TextSegment | None],
    tts: TtsPort,
    accept_audio_segment: AcceptAudioSegment,
    is_response_current: Callable[[], bool],
    record_stage: RecordStage | None,
    get_response_state: Callable[[], ResponseState] | None,
    audit: ProviderResultAudit | None,
) -> None:
    stage_started = False
    audio_sequence = 0
    try:
        while is_response_current():
            text_segment = await queue.get()
            try:
                if text_segment is None:
                    break
                if not stage_started:
                    await _record_stage(record_stage, "tts", "started")
                    stage_started = True
                if not is_response_current():
                    break
                async for synthesized in tts.synthesize(text_segment.text):
                    state = get_response_state() if get_response_state else response.state
                    if audit is not None:
                        audit.audio(
                            synthesized.audio,
                            cancelled=state is ResponseState.CANCELLED,
                            stopping=state is ResponseState.CANCELLING,
                        )
                    audio_sequence += 1
                    local_start, local_end = synthesized.text_range
                    global_range = (
                        text_segment.text_range[0] + local_start,
                        text_segment.text_range[0] + local_end,
                    )
                    await accept_audio_segment(
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
            await _record_stage(record_stage, "tts", "cancelled")
        raise
    except DeliveryError:
        if stage_started:
            await _record_stage(record_stage, "tts", "cancelled")
        raise
    except Exception:
        if stage_started:
            await _record_stage(record_stage, "tts", "failed")
        raise
    if not stage_started:
        return
    if not is_response_current():
        await _record_stage(record_stage, "tts", "cancelled")
        return
    await _record_stage(record_stage, "tts", "completed")


@asynccontextmanager
async def _measure_llm_stage(
    response: Response, record_diagnostic: RecordDiagnostic | None
) -> AsyncIterator[None]:
    with collect_diagnostics() as diagnostics:
        try:
            yield
        finally:
            if record_diagnostic is not None:
                for event in diagnostics.finish():
                    await record_diagnostic(event)


async def run_llm_stage(
    response: Response,
    response_input: str,
    *,
    llm: LlmPort,
    accept_text_delta: AcceptTextDelta,
    is_response_current: Callable[[], bool],
    record_stage: RecordStage | None = None,
    record_diagnostic: RecordDiagnostic | None = None,
) -> bool:
    await _record_stage(record_stage, "llm", "started")
    try:
        async with _measure_llm_stage(response, record_diagnostic):
            async for delta in llm.generate(response_input):
                await accept_text_delta(
                    response_id=response.response_id,
                    generation=response.generation,
                    text_sequence=delta.text_sequence,
                    text=delta.text,
                    text_range=delta.text_range,
                )
    except asyncio.CancelledError:
        await _record_stage(record_stage, "llm", "cancelled")
        raise
    except Exception:
        await _record_stage(record_stage, "llm", "failed")
        return False
    if not is_response_current():
        await _record_stage(record_stage, "llm", "cancelled")
        return False
    await _record_stage(record_stage, "llm", "completed")
    return True


async def run_tts_stage(
    response: Response,
    *,
    tts: TtsPort,
    accept_audio_segment: AcceptAudioSegment,
    is_response_current: Callable[[], bool],
    record_stage: RecordStage | None = None,
) -> bool:
    await _record_stage(record_stage, "tts", "started")
    try:
        async for segment in tts.synthesize(response.generated_text):
            await accept_audio_segment(
                response_id=response.response_id,
                generation=response.generation,
                audio_sequence=segment.audio_sequence,
                audio=segment.audio,
                text_range=segment.text_range,
            )
    except asyncio.CancelledError:
        await _record_stage(record_stage, "tts", "cancelled")
        raise
    except Exception:
        await _record_stage(record_stage, "tts", "failed")
        return False
    if not is_response_current():
        await _record_stage(record_stage, "tts", "cancelled")
        return False
    await _record_stage(record_stage, "tts", "completed")
    return True
