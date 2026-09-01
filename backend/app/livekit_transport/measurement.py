from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Literal
from uuid import uuid4

from app.conversation_core import StageObservation
from app.voice_metrics import EventOutcome, MeasurementKind, TraceEvent

logger = logging.getLogger(__name__)
TraceUnit = Literal["nanosecond", "millisecond"]


@dataclass(frozen=True)
class _PendingTraceEvent:
    event_id: str
    name: str
    stage: str
    outcome: EventOutcome
    reason_code: str | None
    timestamp: int | float
    clock_domain: str
    unit: TraceUnit


class LiveKitMeasurementSession:
    """LiveKit/Coreの観測を#17の1発話単位traceへ相関する。"""

    def __init__(
        self,
        *,
        session_id: str,
        measurement_kind: MeasurementKind,
        record: Callable[[TraceEvent], None] | None,
        clock_ns: Callable[[], int],
    ) -> None:
        self.session_id = session_id
        self._measurement_kind = measurement_kind
        self._record = record
        self._clock_ns = clock_ns
        self._utterance_events: dict[str, list[_PendingTraceEvent]] = {}
        self._response_events: dict[str, list[_PendingTraceEvent]] = {}
        self._response_utterances: dict[str, tuple[str, ...]] = {}
        self._seen_keys: set[tuple[str, str, str]] = set()
        self._seen_client_event_ids: set[str] = set()
        self._recorded_names: set[tuple[str, str, str]] = set()

    def bind_response(
        self, *, response_id: str, source_utterance_ids: tuple[str, ...]
    ) -> None:
        if not source_utterance_ids:
            raise ValueError("response measurement requires a source utterance")
        existing = self._response_utterances.get(response_id)
        if existing is not None and existing != source_utterance_ids:
            raise ValueError("response measurement binding cannot change")
        self._response_utterances[response_id] = source_utterance_ids
        for utterance_id in source_utterance_ids:
            for event in self._utterance_events.get(utterance_id, ()):
                self._emit(event, utterance_id=utterance_id, response_id=response_id)
        for event in self._response_events.get(response_id, ()):
            for utterance_id in source_utterance_ids:
                self._emit(event, utterance_id=utterance_id, response_id=response_id)

    def bind_utterance_outcome(
        self,
        *,
        utterance_id: str,
        outcome: Literal["failure", "excluded"],
        reason_code: str,
    ) -> str:
        response_id = next(
            (
                candidate
                for candidate, source_ids in self._response_utterances.items()
                if utterance_id in source_ids
            ),
            None,
        )
        if response_id is None:
            # STT失敗等はprotocol上のresponseを生成しないため、trace相関専用IDを使う。
            response_id = str(uuid4())
            self.bind_response(
                response_id=response_id,
                source_utterance_ids=(utterance_id,),
            )
        if outcome == "excluded":
            self.record_response_event(
                response_id=response_id,
                name="response_excluded",
                stage="response",
                outcome="excluded",
                reason_code=reason_code,
            )
        return response_id

    def record_utterance_event(
        self,
        *,
        utterance_id: str,
        name: str,
        stage: str,
        outcome: EventOutcome = "success",
        reason_code: str | None = None,
        timestamp: int | float | None = None,
        clock_domain: str = "server_monotonic",
        unit: TraceUnit = "nanosecond",
        event_id: str | None = None,
    ) -> None:
        recorded_name = ("utterance", utterance_id, name)
        if recorded_name in self._recorded_names:
            return
        self._recorded_names.add(recorded_name)
        event = self._pending_event(
            name=name,
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
            timestamp=timestamp,
            clock_domain=clock_domain,
            unit=unit,
            event_id=event_id,
        )
        self._utterance_events.setdefault(utterance_id, []).append(event)
        for response_id, source_utterance_ids in self._response_utterances.items():
            if utterance_id in source_utterance_ids:
                self._emit(event, utterance_id=utterance_id, response_id=response_id)

    def record_response_event(
        self,
        *,
        response_id: str,
        name: str,
        stage: str,
        outcome: EventOutcome = "success",
        reason_code: str | None = None,
        timestamp: int | float | None = None,
        clock_domain: str = "server_monotonic",
        unit: TraceUnit = "nanosecond",
        event_id: str | None = None,
    ) -> None:
        recorded_name = ("response", response_id, name)
        if recorded_name in self._recorded_names:
            return
        self._recorded_names.add(recorded_name)
        event = self._pending_event(
            name=name,
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
            timestamp=timestamp,
            clock_domain=clock_domain,
            unit=unit,
            event_id=event_id,
        )
        self._response_events.setdefault(response_id, []).append(event)
        for utterance_id in self._response_utterances.get(response_id, ()):
            self._emit(event, utterance_id=utterance_id, response_id=response_id)

    async def record(self, observation: StageObservation) -> None:
        logger.info(
            "Conversation Core stage: session_id=%s response_id=%s generation=%s stage=%s outcome=%s utterance_id=%s",
            observation.session_id,
            observation.response_id,
            observation.generation,
            observation.stage,
            observation.outcome,
            observation.utterance_id,
        )
        if observation.session_id != self.session_id:
            raise ValueError("stage observation session does not match measurement session")
        mapped = self._map_stage_observation(observation)
        if mapped is None:
            return
        name, stage, outcome, reason_code = mapped
        if observation.utterance_id is not None:
            self.record_utterance_event(
                utterance_id=observation.utterance_id,
                name=name,
                stage=stage,
                outcome=outcome,
                reason_code=reason_code,
            )
            if observation.stage == "stt" and observation.outcome in {
                "failed",
                "cancelled",
            }:
                self.bind_utterance_outcome(
                    utterance_id=observation.utterance_id,
                    outcome=(
                        "failure"
                        if observation.outcome == "failed"
                        else "excluded"
                    ),
                    reason_code=reason_code or "stt_terminated",
                )
        elif observation.response_id is not None:
            self.record_response_event(
                response_id=observation.response_id,
                name=name,
                stage=stage,
                outcome=outcome,
                reason_code=reason_code,
            )

    def record_client_observation(self, event: dict[str, object]) -> bool:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in self._seen_client_event_ids:
            return False
        if event.get("session_id") != self.session_id:
            return False
        measurement = event.get("measurement")
        timestamp = event.get("timestamp")
        if (
            measurement not in {"speech_stopped", "playback_started"}
            or type(timestamp) is not int
            or event.get("clock_domain") != "client_monotonic"
            or event.get("unit") != "millisecond"
        ):
            return False
        self._seen_client_event_ids.add(event_id)
        if measurement == "speech_stopped":
            utterance_id = event.get("utterance_id")
            if (
                not isinstance(utterance_id, str)
                or utterance_id not in self._utterance_events
            ):
                return False
            self.record_utterance_event(
                utterance_id=utterance_id,
                name="speech_stopped",
                stage="vad",
                timestamp=timestamp,
                clock_domain="client_monotonic",
                unit="millisecond",
                event_id=event_id,
            )
            return True
        response_id = event.get("response_id")
        if not isinstance(response_id, str) or response_id not in self._response_utterances:
            return False
        self.record_response_event(
            response_id=response_id,
            name="first_playback",
            stage="playback",
            timestamp=timestamp,
            clock_domain="client_monotonic",
            unit="millisecond",
            event_id=event_id,
        )
        return True

    def _pending_event(
        self,
        *,
        name: str,
        stage: str,
        outcome: EventOutcome,
        reason_code: str | None,
        timestamp: int | float | None,
        clock_domain: str,
        unit: TraceUnit,
        event_id: str | None,
    ) -> _PendingTraceEvent:
        return _PendingTraceEvent(
            event_id=event_id or str(uuid4()),
            name=name,
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
            timestamp=self._clock_ns() if timestamp is None else timestamp,
            clock_domain=clock_domain,
            unit=unit,
        )

    def _emit(
        self, event: _PendingTraceEvent, *, utterance_id: str, response_id: str
    ) -> None:
        if self._record is None:
            return
        dedupe_key = (event.event_id, utterance_id, response_id)
        if dedupe_key in self._seen_keys:
            return
        self._seen_keys.add(dedupe_key)
        self._record(TraceEvent(
            schema_version="1.0",
            measurement_kind=self._measurement_kind,
            event_id=event.event_id,
            session_id=self.session_id,
            utterance_id=utterance_id,
            response_id=response_id,
            name=event.name,
            stage=event.stage,
            outcome=event.outcome,
            reason_code=event.reason_code,
            timestamp=event.timestamp,
            clock_domain=event.clock_domain,
            unit=event.unit,
        ))

    @staticmethod
    def _map_stage_observation(
        observation: StageObservation,
    ) -> tuple[str, str, EventOutcome, str | None] | None:
        stage = "transport" if observation.stage == "delivery" else observation.stage
        if stage not in {"stt", "llm", "tts", "transport"}:
            return None
        if stage == "transport" and observation.outcome != "failed":
            return None
        if stage == "tts" and observation.outcome == "completed":
            # first audio生成はdeliveryが正確に記録する。ここはpipeline終端なので別名にする。
            return "tts_pipeline_completed", stage, "success", None
        suffix = {
            "started": "started",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(observation.outcome)
        if suffix is None:
            return None
        outcome: EventOutcome = (
            "failure" if observation.outcome == "failed"
            else "excluded" if observation.outcome == "cancelled"
            else "success"
        )
        reason_code = (
            f"{stage}_{suffix}" if outcome != "success" else None
        )
        return f"{stage}_{suffix}", stage, outcome, reason_code
