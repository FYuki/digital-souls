from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Literal, cast
from uuid import uuid4

from app.conversation_core import StageObservation
from app.conversation_core.provider_result_audit import PROVIDER_RESULT_METRICS, PROVIDER_STOPPING_METRICS
from app.inference.diagnostics import DIAGNOSTIC_NAMES
from app.voice_metrics import EventOutcome, MeasurementKind, TraceEvent
from app.livekit_transport.playback_summary import validate_playback_summary
from app.voice_network_metrics import network_summary_values

logger = logging.getLogger(__name__)
TraceUnit = Literal["nanosecond", "millisecond"]
_MAX_PENDING_CLIENT_OBSERVATIONS = 256
_INTERRUPT_NAMES = frozenset({
    "interruption_started", "speech_started", "speech_started_client",
    "turn_decision", "take_turn_decision", "server_cancelled",
    "local_playback_stopped", "turn_decision_client", "server_cancelled_client",
})
_CLIENT_INTERRUPT_NAMES = {
    "turn_decision_received": "turn_decision_client",
    "cancel_confirmed": "server_cancelled_client",
    "local_playback_stopped": "local_playback_stopped",
}

_CLIENT_MEDIA_NAMES = {
    "client_track_received": "client_track_received",
    "client_encoded_received": "client_audio_received",
    "client_audio_decoded": "client_audio_decoded",
}


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
    value: float | None


class LiveKitMeasurementSession:
    """LiveKit/Coreの観測を#17の1発話単位traceへ相関する。"""

    def __init__(
        self,
        *,
        session_id: str,
        character_id: str,
        measurement_kind: MeasurementKind,
        record: Callable[[TraceEvent], None] | None,
        clock_ns: Callable[[], int],
    ) -> None:
        self.session_id = session_id
        self._character_id = character_id
        self._measurement_kind = measurement_kind
        self._record = record
        self._clock_ns = clock_ns
        self._utterance_events: dict[str, list[_PendingTraceEvent]] = {}
        self._response_events: dict[str, list[_PendingTraceEvent]] = {}
        self._response_utterances: dict[str, tuple[str, ...]] = {}
        self._interruption_responses: dict[str, str] = {}
        self._seen_keys: set[tuple[str, str, str]] = set()
        self._seen_client_event_ids: set[str] = set()
        self._pending_client_observations: dict[str, dict[str, object]] = {}
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
                if event.name not in _INTERRUPT_NAMES:
                    self._emit(event, utterance_id=utterance_id, response_id=response_id)
        for event in self._response_events.get(response_id, ()):
            for utterance_id in source_utterance_ids:
                self._emit(event, utterance_id=utterance_id, response_id=response_id)
        self._retry_pending_client_observations()

    def bind_interruption(self, *, utterance_id: str, response_id: str) -> bool:
        if response_id not in self._response_utterances:
            return False
        existing = self._interruption_responses.get(utterance_id)
        if existing is not None:
            return existing == response_id
        self._interruption_responses[utterance_id] = response_id
        self.record_utterance_event(
            utterance_id=utterance_id, name="interruption_started", stage="turn",
        )
        for event in self._utterance_events.get(utterance_id, ()):
            if event.name in _INTERRUPT_NAMES:
                self._emit(event, utterance_id=utterance_id, response_id=response_id)
        self._retry_pending_client_observations()
        return True

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
        value: float | None = None,
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
            value=value,
        )
        self._utterance_events.setdefault(utterance_id, []).append(event)
        if name in _INTERRUPT_NAMES:
            interrupted_response = self._interruption_responses.get(utterance_id)
            if interrupted_response is not None:
                self._emit(event, utterance_id=utterance_id, response_id=interrupted_response)
        else:
            for response_id, source_utterance_ids in self._response_utterances.items():
                if utterance_id in source_utterance_ids:
                    self._emit(event, utterance_id=utterance_id, response_id=response_id)
        self._retry_pending_client_observations()

    def record_playback_summary(self, *, response_id: str, summary: object) -> bool:
        """完了gateが受理した応答だけを、既存の送信量と照合して記録する。"""
        if response_id not in self._response_utterances or not isinstance(summary, dict):
            return False
        source_samples = {
            event.name: event.value for event in self._response_events.get(response_id, ())
            if event.outcome == "success"
        }
        try:
            values = validate_playback_summary(summary, source_samples)
        except ValueError:
            self.record_response_event(
                response_id=response_id, name="playback_summary_invalid", stage="measurement",
                outcome="excluded", reason_code="playback_summary_source_or_clock_mismatch",
            )
            return False
        for name, frame in (
            ("scheduled_playout", summary["first_output_frame"] + summary["expected_samples"]),
            ("frame_playout", summary["last_output_end_frame"]),
        ):
            self.record_response_event(
                response_id=response_id, name=name, stage="playback", timestamp=frame / 48,
                clock_domain="browser_audio_context", unit="millisecond",
            )
        for name, value in values.items():
            self.record_response_event(
                response_id=response_id, name=name, stage="playback", value=value,
                timestamp=summary["confirmation_observed_at_ms"],
                clock_domain="client_monotonic", unit="millisecond",
            )
        return True

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
        value: float | None = None,
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
            value=value,
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
                timestamp=observation.timestamp_ns,
                value=observation.value,
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
                timestamp=observation.timestamp_ns,
                value=observation.value,
            )

    def record_client_observation(self, event: dict[str, object]) -> bool:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in self._seen_client_event_ids:
            return False
        if event.get("session_id") != self.session_id:
            return False
        measurement = event.get("measurement")
        timestamp = event.get("timestamp")
        if measurement == "network_summary":
            response_id = event.get("response_id")
            if (not isinstance(response_id, str) or response_id not in self._response_utterances
                    or type(timestamp) is not int or timestamp < 0
                    or event.get("clock_domain") != "client_monotonic" or event.get("unit") != "millisecond"
                    or ("response", response_id, "network_rtp_summary_recorded") in self._recorded_names):
                return False
            samples = next((item.value for item in self._response_events.get(response_id, ())
                            if item.name == "response_audio_captured_samples" and item.outcome == "success"), None)
            raw = event.get("network_summary")
            cancelled = isinstance(raw, dict) and raw.get("boundary") == "response_cancelled"
            if cancelled:
                # clientの申告だけで完了応答のpacket下限を緩めない。Coreの取消記録が必要。
                if not any(item.name == "response_cancelled" and item.outcome == "excluded"
                           for item in self._response_events.get(response_id, ())):
                    return False
                expected_packets = 0
            else:
                if samples is None or samples < 0 or samples % 960:
                    return False
                expected_packets = int(samples) // 960
            try:
                values = network_summary_values(raw, expected_packets=expected_packets)
            except ValueError:
                return False
            self._seen_client_event_ids.add(event_id)
            for name, value in values.items():
                self.record_response_event(response_id=response_id, name=name, stage="network", value=value,
                                           timestamp=timestamp, clock_domain="client_monotonic", unit="millisecond")
            return True
        if (
            measurement not in {"speech_stopped", "playback_started", *_CLIENT_INTERRUPT_NAMES, *_CLIENT_MEDIA_NAMES}
            or type(timestamp) is not int
            or event.get("clock_domain") != "client_monotonic"
            or event.get("unit") != "millisecond"
        ):
            return False
        if measurement == "speech_stopped":
            utterance_id = event.get("utterance_id")
            if not isinstance(utterance_id, str):
                return False
            normalized = {
                "event_id": event_id,
                "measurement": measurement,
                "utterance_id": utterance_id,
                "timestamp": timestamp,
            }
        else:
            response_id = event.get("response_id")
            if not isinstance(response_id, str):
                return False
            normalized = {
                "event_id": event_id,
                "measurement": measurement,
                "response_id": response_id,
                "timestamp": timestamp,
            }
            if measurement in _CLIENT_INTERRUPT_NAMES:
                utterance_id = event.get("utterance_id")
                if not isinstance(utterance_id, str):
                    return False
                normalized["utterance_id"] = utterance_id

        pending = self._pending_client_observations.get(event_id)
        if pending is not None and pending != normalized:
            return False
        if not self._client_observation_is_correlated(normalized):
            if pending is None and (
                len(self._pending_client_observations)
                < _MAX_PENDING_CLIENT_OBSERVATIONS
            ):
                self._pending_client_observations[event_id] = normalized
            return False

        self._pending_client_observations.pop(event_id, None)
        self._seen_client_event_ids.add(event_id)
        self._record_correlated_client_observation(normalized)
        return True

    def _client_observation_is_correlated(self, event: dict[str, object]) -> bool:
        if event["measurement"] == "speech_stopped":
            return event["utterance_id"] in self._utterance_events
        if event["measurement"] in _CLIENT_INTERRUPT_NAMES:
            return self._interruption_responses.get(str(event["utterance_id"])) == event["response_id"]
        return event["response_id"] in self._response_utterances

    def _record_correlated_client_observation(
        self, event: dict[str, object]
    ) -> None:
        event_id = str(event["event_id"])
        timestamp = cast(int, event["timestamp"])
        if event["measurement"] in _CLIENT_INTERRUPT_NAMES:
            self.record_utterance_event(
                utterance_id=str(event["utterance_id"]),
                name=_CLIENT_INTERRUPT_NAMES[str(event["measurement"])], stage="turn",
                timestamp=timestamp, clock_domain="client_monotonic",
                unit="millisecond", event_id=event_id,
            )
            return
        if event["measurement"] == "speech_stopped":
            self.record_utterance_event(
                utterance_id=str(event["utterance_id"]),
                name="speech_stopped",
                stage="vad",
                timestamp=timestamp,
                clock_domain="client_monotonic",
                unit="millisecond",
                event_id=event_id,
            )
            return
        self.record_response_event(
            response_id=str(event["response_id"]),
            name=_CLIENT_MEDIA_NAMES.get(str(event["measurement"]), "first_playback"),
            stage="transport" if event["measurement"] in _CLIENT_MEDIA_NAMES else "playback",
            timestamp=timestamp,
            clock_domain="client_monotonic",
            unit="millisecond",
            event_id=event_id,
        )

    def _retry_pending_client_observations(self) -> None:
        for event_id, event in tuple(self._pending_client_observations.items()):
            if not self._client_observation_is_correlated(event):
                continue
            # 再入時に同じeventを処理しないよう、記録処理より先にpendingから外す。
            self._pending_client_observations.pop(event_id)
            self._seen_client_event_ids.add(event_id)
            self._record_correlated_client_observation(event)

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
        value: float | None,
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
            value=value,
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
            character_id=self._character_id,
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
            value=event.value,
        ))

    @staticmethod
    def _map_stage_observation(
        observation: StageObservation,
    ) -> tuple[str, str, EventOutcome, str | None] | None:
        if observation.stage in PROVIDER_STOPPING_METRICS:
            if observation.outcome != 'completed':
                return None
            return observation.stage, 'provider_result_stopping', 'success', None
        if observation.stage in PROVIDER_RESULT_METRICS:
            if observation.outcome != 'completed':
                return None
            return observation.stage, 'provider_result_received', 'success', None
        if observation.stage in DIAGNOSTIC_NAMES:
            return observation.stage, "inference_diagnostic", "success", None
        stage = "transport" if observation.stage == "delivery" else observation.stage
        if stage in {"turn_decision", "take_turn_decision", "server_cancelled"}:
            if observation.outcome != "completed":
                return None
            return stage, "turn", "success", None
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
