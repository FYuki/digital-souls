"""外部正本の照会とTask追跡。保存済み結果を再取得しても副作用を新規送信しない。"""

from __future__ import annotations

import asyncio
import json
from weakref import WeakValueDictionary
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.external_mcp.models import MCPFailure
from app.tool_use.projection import bounded_json

from .dispatch import ActionDispatch
from .journal import ActionOutcome, ActionRecord, UNRESOLVED
from .models import ExecutionScene
from .recovery_contract import decode_action

if TYPE_CHECKING:
    from app.external_mcp.gate import ExecutionGate


class ActionRecovery:
    def __init__(self, gate: ExecutionGate, actions: ActionDispatch) -> None:
        self.gate = gate
        self.actions = actions
        self.autonomous_guard: Callable[[ActionRecord], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._cursor = 0

    def changed(self, record: ActionRecord) -> None:
        if record.outcome in UNRESOLVED:
            self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._track())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def _guard(self, record: ActionRecord) -> None:
        if record.identity.scene == ExecutionScene.AUTONOMOUS:
            if self.autonomous_guard is None:
                raise MCPFailure("policy", "autonomy_recovery_unavailable")
            self.autonomous_guard(record)

    async def _query(self, record: ActionRecord, method: str) -> ActionRecord:
        try:
            async with asyncio.timeout(15):
                payload = await self.gate.recovery_call(
                    record,
                    method,
                    guard=lambda: self._guard(record),
                )
            outcome, task_id, external_result = decode_action(payload)
            # replayは保存済み結果の再返却だけを保証する契約。未実行を実行へ進めない。
            if method == "replay" and outcome in UNRESOLVED:
                return record
            projected = {
                "outcome": self.actions.envelope_outcome(outcome),
                "structured": self.actions.sanitizer.value(external_result),
            }
            return self.actions.journal.finish(
                record.execution_id,
                outcome,
                task_id=task_id,
                projection=json.loads(bounded_json(projected, 16_384)),
            )
        except (MCPFailure, TimeoutError):
            # 照会不能は外部実行の失敗ではない。最後に確認した状態を保つ。
            return self.actions.journal.get(record.execution_id)

    async def recover(self, execution_id: str) -> str:
        lock = self._locks.setdefault(execution_id, asyncio.Lock())
        async with lock:
            record = self.actions.journal.get(execution_id)
            needs_latest = (
                record.outcome == ActionOutcome.CONFLICT
                and "latest_state" not in record.projection.get("structured", {})
            )
            if (
                record.outcome not in UNRESOLVED and not needs_latest
            ) or not record.identity.recovery_json:
                return record.outcome.value
            record = await self._query(record, "status")
            assert record.identity.recovery_json is not None
            profile = json.loads(record.identity.recovery_json)
            if record.outcome == ActionOutcome.RESULT_UNKNOWN and "replay" in profile:
                record = await self._query(record, "replay")
            return record.outcome.value

    def _task_record(self, connection_id: str, task_id: str) -> ActionRecord:
        records = self.actions.journal.for_task(connection_id, task_id)
        if len(records) != 1:
            raise MCPFailure("recovery", "unknown_or_ambiguous_task")
        return records[0]

    async def status(self, connection_id: str, task_id: str) -> str:
        return await self.recover(
            self._task_record(connection_id, task_id).execution_id
        )

    async def cancel(self, connection_id: str, task_id: str) -> str:
        record = self._task_record(connection_id, task_id)
        return await self.cancel_execution(record.execution_id)

    async def cancel_execution(self, execution_id: str) -> str:
        lock = self._locks.setdefault(execution_id, asyncio.Lock())
        async with lock:
            record = self.actions.journal.request_cancel(execution_id)
            if record.outcome not in UNRESOLVED:
                return record.outcome.value
            if not record.identity.recovery_json or "cancel" not in json.loads(
                record.identity.recovery_json
            ):
                return "unsupported"
            return (await self._query(record, "cancel")).outcome.value

    async def _track(self) -> None:
        while True:
            self._wake.clear()
            pending = [
                r for r in self.actions.journal.pending() if r.identity.recovery_json
            ]
            # 同時照会数と1巡の件数を制限し、多数のTaskで会話の予算を占有しない。
            if pending:
                start = self._cursor % len(pending)
                records = (pending[start:] + pending[:start])[:4]
                self._cursor += len(records)
                await asyncio.gather(
                    *(self._track_one(r) for r in records), return_exceptions=True
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            except TimeoutError:
                pass

    async def _track_one(self, record: ActionRecord) -> None:
        if record.cancel_requested and record.outcome != ActionOutcome.CANCEL_REQUESTED:
            await self.cancel_execution(record.execution_id)
        # cancel非対応や応答不明でも外部正本の追跡は継続する。
        await self.recover(record.execution_id)
