"""送信記録から安全な結果投影だけを返す。外部の再送契約とは分離する。"""

from __future__ import annotations

import json
from collections.abc import Callable

from app.external_mcp.models import Json
from app.tool_use.projection import Sanitizer, bounded_json

from .journal import ActionJournal, ActionOutcome, ActionRecord
from .recovery_contract import decode_action


class ActionDispatch:
    def __init__(self, journal: ActionJournal, sanitizer: Sanitizer) -> None:
        self.journal = journal
        self.sanitizer = sanitizer
        self.on_change: Callable[[ActionRecord], None] = lambda _record: None

    def finish(self, execution_id: str, envelope: Json) -> ActionRecord:
        record = self.journal.get(execution_id)
        if (
            record.identity.recovery_json
            and envelope.get("native_payload")
            and envelope["outcome"] != "input_required"
        ):
            outcome, task_id, external_result = decode_action(
                envelope["native_payload"]
            )
            envelope["outcome"] = self.envelope_outcome(outcome)
            projected = {
                "outcome": envelope["outcome"],
                "structured": self.sanitizer.value(external_result),
            }
            envelope["result_projection"] = projected
        else:
            outcome = {
                "succeeded": ActionOutcome.APPLIED,
                "input_required": ActionOutcome.INPUT_REQUIRED,
                "failed": ActionOutcome.FAILED,
                "conflict": ActionOutcome.CONFLICT,
                "no_change": ActionOutcome.NO_CHANGE,
            }.get(envelope["outcome"], ActionOutcome.RESULT_UNKNOWN)
            task_id = None
            # MRTRのopaque stateは永続化せず、再起動後に要求を作り直して再送しない。
            projected = self.sanitizer.result(envelope, may_change_state=False)
        observed = self.journal.finish(
            execution_id,
            outcome,
            task_id=task_id,
            projection=json.loads(bounded_json(projected, 16_384)),
        )
        if observed.outcome != outcome:
            # 先に外部正本の照会で確定した結果を、遅い元応答で戻さない。
            envelope["outcome"] = self.envelope_outcome(observed.outcome)
            envelope["result_projection"] = observed.projection
        return observed

    @staticmethod
    def envelope_outcome(outcome: ActionOutcome) -> str:
        return {
            ActionOutcome.APPLIED: "succeeded",
            ActionOutcome.NO_CHANGE: "no_change",
            ActionOutcome.CONFLICT: "conflict",
            ActionOutcome.FAILED: "failed",
            ActionOutcome.CANCELLED: "cancelled",
            ActionOutcome.RUNNING: "running",
            ActionOutcome.CANCEL_REQUESTED: "cancel_requested",
            ActionOutcome.INPUT_REQUIRED: "deferred",
        }.get(outcome, "result_unknown")

    def previous(self, record: ActionRecord, envelope: Json) -> Json:
        outcome = self.envelope_outcome(record.outcome)
        return {
            **envelope,
            "execution_id": record.execution_id,
            "outcome": outcome,
            "replayed": True,
            "native_payload": None,
            # 保存後のsecret更新も表示前に再適用する。
            "result_projection": self.sanitizer.value(record.projection),
        }
