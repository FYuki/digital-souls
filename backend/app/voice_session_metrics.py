"""応答の有無に依存しないsession計測。本文や架空のutterance IDは保存しない。"""
from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.voice_metrics import MeasurementKind

logger = logging.getLogger(__name__)
Count = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]
EndReason = Literal[
    "explicit", "protocol_error", "runtime_error", "reconnect_timeout",
    "join_token_expired", "unknown",
]


class SessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    sequence: Count
    microphone_activation_attempts: Count
    mute_attempts: Count
    retry_attempts: Count
    operation_tracking_started: bool
    end_requested: bool

    @property
    def additional_operations(self) -> int:
        # 最初の開始操作だけを除外し、失敗した再試行も残す。
        return max(0, self.microphone_activation_attempts - 1) + self.mute_attempts + self.retry_attempts


class SessionTraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    measurement_kind: MeasurementKind
    character_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    sequence: Count
    timestamp_ns: Annotated[int, Field(strict=True, ge=0)]
    clock_domain: Literal["server_monotonic"] = "server_monotonic"
    name: Literal["created", "activated", "summary", "playback_completed", "ended", "cleanup_failed", "invalid_summary", "overflow"]
    summary: SessionSummary | None = None
    response_id: str | None = Field(default=None, min_length=1)
    reason: EndReason | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> SessionTraceEvent:
        if (self.name == "summary") != (self.summary is not None):
            raise ValueError("summary shape mismatch")
        if (self.name == "playback_completed") != (self.response_id is not None):
            raise ValueError("playback shape mismatch")
        if (self.name in {"ended", "cleanup_failed"}) != (self.reason is not None):
            raise ValueError("end reason shape mismatch")
        return self


class SessionMetrics:
    """runtime所有の記録器。再送は加算せず、累積値の逆行は欠測にする。"""

    def __init__(self, *, character_id: str, session_id: str, measurement_kind: MeasurementKind,
                 record: Callable[[SessionTraceEvent], None], clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._character_id = character_id
        self._session_id = session_id
        self._kind = measurement_kind
        self._record = record
        self._clock = clock_ns
        self._sequence = 0
        self._active = False
        self._ended = False
        self._summary: SessionSummary | None = None
        self._summaries: dict[int, SessionSummary] = {}
        self._responses: set[str] = set()
        self._overflow = False
        self._invalid = False
        self._emit("created")

    def _emit(self, name: str, **fields: object) -> None:
        event = SessionTraceEvent.model_validate(dict(
            measurement_kind=self._kind, character_id=self._character_id,
            session_id=self._session_id, sequence=self._sequence,
            timestamp_ns=self._clock(), name=name, **fields,
        ))
        self._sequence += 1
        try:
            self._record(event)
        except Exception:
            # 記録失敗で会話を中断しない。連番の欠落は集計時に欠測として残る。
            logger.warning("Voice session metrics write failed")

    def activate(self) -> None:
        if not self._active and not self._ended:
            self._active = True
            self._emit("activated")

    def observe_summary(self, value: object) -> None:
        if self._ended or self._invalid or self._overflow:
            return
        try:
            summary = SessionSummary.model_validate(value)
            if self._summaries.get(summary.sequence) == summary:
                return
            previous = self._summary
            if previous is not None:
                if summary == previous:
                    return
                if summary.sequence <= previous.sequence or previous.end_requested:
                    raise ValueError("summary sequence reversed or closed")
                for field in ("microphone_activation_attempts", "mute_attempts", "retry_attempts"):
                    if getattr(summary, field) < getattr(previous, field):
                        raise ValueError("counter decreased")
                if previous.operation_tracking_started and not summary.operation_tracking_started:
                    raise ValueError("tracking reversed")
            if self._sequence >= 4096:
                self._overflow = True
                self._emit("overflow")
                return
            self._summary = summary
            self._summaries[summary.sequence] = summary
            self._emit("summary", summary=summary)
        except ValueError:
            self._invalid = True
            self._emit("invalid_summary")

    def completed_playback(self, response_id: str) -> None:
        if self._ended or response_id in self._responses or self._overflow:
            return
        if len(self._responses) >= 1024:
            self._overflow = True
            self._emit("overflow")
            return
        self._responses.add(response_id)
        self._emit("playback_completed", response_id=response_id)

    def end(self, reason: str, *, cleanup_completed: bool = True) -> None:
        if self._ended:
            return
        self._ended = True
        allowed = {"explicit", "protocol_error", "runtime_error", "reconnect_timeout", "join_token_expired"}
        self._emit("ended" if cleanup_completed else "cleanup_failed",
                   reason=reason if reason in allowed else "unknown")


class OperationAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    measured_sessions: Count
    values: list[Count]

    @model_validator(mode="after")
    def validate_denominator(self) -> OperationAggregate:
        if self.measured_sessions != len(self.values):
            raise ValueError("operation denominator mismatch")
        return self


class SessionAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    measurement_kind: MeasurementKind
    scope: Literal["runtime_sessions_including_zero_response"] = "runtime_sessions_including_zero_response"
    expected_sessions: Annotated[int, Field(strict=True, ge=1)]
    observed_sessions: Count
    counts: dict[Literal["created_sessions", "activated_sessions", "never_activated_sessions",
                         "ended_sessions", "zero_completed_response_sessions", "expected_ends",
                         "unexpected_ends"], Count]
    end_reasons: dict[EndReason, Count]
    missing_sessions_by_reason: dict[Literal["incomplete_or_invalid_journal",
        "session_end_not_recorded_or_cleanup_failed", "end_intent_not_confirmed",
        "complete_operation_window_not_recorded", "session_journal_absent"], Count]
    additional_operations: OperationAggregate
    three_turn_additional_operations: OperationAggregate
    full_issue_acceptance_verified: Literal[False] = False

    @model_validator(mode="after")
    def validate_population(self) -> SessionAggregate:
        if self.observed_sessions > self.expected_sessions:
            raise ValueError("session denominator mismatch")
        if self.missing_sessions_by_reason.get("session_journal_absent", 0) != self.expected_sessions - self.observed_sessions:
            raise ValueError("missing session denominator mismatch")
        if any(value > self.observed_sessions for value in self.counts.values()):
            raise ValueError("session count exceeds population")
        if self.additional_operations.measured_sessions > self.counts.get("activated_sessions", 0):
            raise ValueError("operation count exceeds active sessions")
        if self.three_turn_additional_operations.measured_sessions > self.additional_operations.measured_sessions:
            raise ValueError("three turn count exceeds operation sessions")
        return self


def aggregate_sessions(events: Sequence[SessionTraceEvent], *, measurement_kind: MeasurementKind,
                       expected_sessions: int) -> dict[str, object]:
    """想定session数は外部の開始記録から与える。空journalを成功0件にしない。"""
    if type(expected_sessions) is not int or expected_sessions < 1:
        raise ValueError("expected_sessions must be positive")
    groups: dict[str, list[SessionTraceEvent]] = {}
    characters: set[str] = set()
    for event in events:
        if event.measurement_kind != measurement_kind:
            raise ValueError("mixed measurement kind")
        characters.add(event.character_id)
        groups.setdefault(event.session_id, []).append(event)
    if len(characters) > 1 or len(groups) > expected_sessions:
        raise ValueError("session population mismatch")
    counts: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    operation_values: list[int] = []
    three_turn_values: list[int] = []
    for rows in groups.values():
        # 複数trace入力の同一行は許容し、同じ連番の矛盾は欠測にする。
        by_sequence: dict[int, SessionTraceEvent] = {}
        corrupt = False
        for row in rows:
            if row.sequence in by_sequence and by_sequence[row.sequence] != row:
                corrupt = True
            by_sequence[row.sequence] = row
        rows = [by_sequence[key] for key in sorted(by_sequence)]
        corrupt |= [row.sequence for row in rows] != list(range(len(rows)))
        corrupt |= not rows or rows[0].name != "created"
        corrupt |= any(b.timestamp_ns < a.timestamp_ns for a, b in zip(rows, rows[1:]))
        corrupt |= any(row.name in {"invalid_summary", "overflow"} for row in rows)
        if not corrupt:
            replayed: list[SessionTraceEvent] = []
            replay = SessionMetrics(character_id=rows[0].character_id, session_id=rows[0].session_id,
                                    measurement_kind=measurement_kind, record=replayed.append, clock_ns=lambda: 0)
            for row in rows[1:]:
                if row.name == "activated":
                    replay.activate()
                elif row.name == "summary":
                    replay.observe_summary(row.summary)
                elif row.name == "playback_completed" and row.response_id is not None:
                    replay.completed_playback(row.response_id)
                elif row.name in {"ended", "cleanup_failed"} and row.reason is not None:
                    replay.end(row.reason, cleanup_completed=row.name == "ended")
            corrupt |= [row.model_dump(exclude={"timestamp_ns"}) for row in rows] != [
                row.model_dump(exclude={"timestamp_ns"}) for row in replayed]
        if corrupt:
            missing["incomplete_or_invalid_journal"] += 1
            continue
        counts["created_sessions"] += 1
        active = any(row.name == "activated" for row in rows)
        if not active:
            counts["never_activated_sessions"] += 1
        else:
            counts["activated_sessions"] += 1
        ended = rows[-1] if rows[-1].name == "ended" else None
        if ended is None:
            missing["session_end_not_recorded_or_cleanup_failed"] += 1
            continue
        counts["ended_sessions"] += 1
        reasons[str(ended.reason)] += 1
        if not active:
            continue
        summary = next((row.summary for row in reversed(rows) if row.summary is not None), None)
        complete_responses = len({row.response_id for row in rows if row.name == "playback_completed"})
        if complete_responses == 0:
            counts["zero_completed_response_sessions"] += 1
        if ended.reason == "explicit" and summary is not None and summary.end_requested:
            counts["expected_ends"] += 1
        elif ended.reason not in {"explicit", "unknown"}:
            counts["unexpected_ends"] += 1
        else:
            missing["end_intent_not_confirmed"] += 1
        if summary is None or not summary.end_requested or not summary.operation_tracking_started:
            missing["complete_operation_window_not_recorded"] += 1
            continue
        operation_values.append(summary.additional_operations)
        if complete_responses >= 3:
            three_turn_values.append(summary.additional_operations)
    missing["session_journal_absent"] += expected_sessions - len(groups)
    return SessionAggregate.model_validate({
        "schema_version": "1.0", "measurement_kind": measurement_kind,
        "scope": "runtime_sessions_including_zero_response",
        "expected_sessions": expected_sessions, "observed_sessions": len(groups),
        "counts": dict(counts), "end_reasons": dict(reasons),
        "missing_sessions_by_reason": {key: value for key, value in missing.items() if value},
        "additional_operations": {"measured_sessions": len(operation_values), "values": operation_values},
        "three_turn_additional_operations": {
            "measured_sessions": len(three_turn_values), "values": three_turn_values,
        },
        "full_issue_acceptance_verified": False,
    }).model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description="応答に依存しないsession集計（匿名）")
    parser.add_argument("--trace", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--measurement-kind", choices=("automated_test", "controlled_baseline", "dogfood"), required=True)
    parser.add_argument("--expected-sessions", type=int, required=True)
    args = parser.parse_args()
    events = [SessionTraceEvent.model_validate_json(line) for path in args.trace
              for line in path.read_text().splitlines() if line.strip()]
    result = aggregate_sessions(events, measurement_kind=args.measurement_kind,
                                expected_sessions=args.expected_sessions)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
