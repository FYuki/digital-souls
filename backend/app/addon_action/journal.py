"""外部副作用の送信境界を永続化する。runtime checkpointだけで再送しない。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from app.external_mcp.models import Json, MCPFailure, encode, now

from .models import ExecutionScene
from .store import ActionStore


class ActionOutcome(StrEnum):
    DISPATCHING = "dispatching"
    APPLIED = "applied"
    NO_CHANGE = "no_change"
    CONFLICT = "conflict"
    FAILED = "failed"
    RESULT_UNKNOWN = "result_unknown"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    INPUT_REQUIRED = "input_required"


UNRESOLVED = frozenset(
    {
        ActionOutcome.DISPATCHING,
        ActionOutcome.RESULT_UNKNOWN,
        ActionOutcome.RUNNING,
        ActionOutcome.CANCEL_REQUESTED,
    }
)


@dataclass(frozen=True)
class ActionIdentity:
    scope: str
    fingerprint: str
    connection_id: str
    connection_identity: str
    character: str
    session: str
    scene: ExecutionScene
    operation: str
    definition_digest: str
    binding_id: str | None = None
    recovery_json: str | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class ActionRecord:
    execution_id: str
    identity: ActionIdentity
    request_key: str
    outcome: ActionOutcome
    task_id: str | None
    cancel_requested: bool
    projection: Json


class UnresolvedAction(MCPFailure):
    def __init__(self, execution_id: str) -> None:
        super().__init__("recovery", "scope_has_unresolved_action")
        self.execution_id = execution_id


class ActionJournal:
    def __init__(self, store: ActionStore) -> None:
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS action_executions (
                execution_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
                connection_id TEXT NOT NULL, connection_identity TEXT NOT NULL,
                character TEXT NOT NULL, session TEXT NOT NULL, scene TEXT NOT NULL,
                operation TEXT NOT NULL, definition_digest TEXT NOT NULL, binding_id TEXT, recovery_json TEXT, user_id TEXT,
                request_key TEXT NOT NULL, outcome TEXT NOT NULL,
                task_id TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                projection TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(character, scene, scope, fingerprint)
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS action_scope ON action_executions(character,scene,scope,outcome)"
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> ActionRecord:
        return ActionRecord(
            row["execution_id"],
            ActionIdentity(
                row["scope"],
                row["fingerprint"],
                row["connection_id"],
                row["connection_identity"],
                row["character"],
                row["session"],
                ExecutionScene(row["scene"]),
                row["operation"],
                row["definition_digest"],
                row["binding_id"],
                row["recovery_json"],
                row["user_id"],
            ),
            row["request_key"],
            ActionOutcome(row["outcome"]),
            row["task_id"],
            bool(row["cancel_requested"]),
            json.loads(row["projection"]),
        )

    def get(self, execution_id: str) -> ActionRecord:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM action_executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            if row is None:
                raise MCPFailure("recovery", "unknown_execution")
            return self._record(row)

    def find(self, identity: ActionIdentity) -> ActionRecord | None:
        with self.store.transaction() as db:
            return self._find(db, identity)

    def _find(
        self, db: sqlite3.Connection, identity: ActionIdentity
    ) -> ActionRecord | None:
        row = db.execute(
            "SELECT * FROM action_executions WHERE character=? AND scene=? AND scope=? AND fingerprint=?",
            (identity.character, identity.scene, identity.scope, identity.fingerprint),
        ).fetchone()
        if row is None:
            return None
        record = self._record(row)
        if record.identity != identity:
            # 同じlogical requestをschema変更・別接続・別対象へ読み替えない。
            raise MCPFailure("recovery", "action_identity_changed")
        return record

    def check(self, identity: ActionIdentity) -> ActionRecord | None:
        with self.store.transaction() as db:
            previous = self._find(db, identity)
            if previous is not None:
                return previous
            self._check_unresolved(db, identity)
            return None

    @staticmethod
    def _check_unresolved(db: sqlite3.Connection, identity: ActionIdentity) -> None:
        rows = db.execute(
            "SELECT execution_id,outcome FROM action_executions WHERE character=? AND scene=? AND scope=?",
            (identity.character, identity.scene, identity.scope),
        )
        unresolved = next((r for r in rows if r["outcome"] in UNRESOLVED), None)
        if unresolved is not None:
            raise UnresolvedAction(unresolved["execution_id"])

    def begin(
        self,
        identity: ActionIdentity,
        execution_id: str,
        *,
        request_key: str | None = None,
    ) -> tuple[ActionRecord, bool]:
        """送信直前にclaimする。並行するruntimeのうち1つだけが新規dispatchできる。"""
        with self.store.transaction() as db:
            previous = self._find(db, identity)
            if previous is not None:
                return previous, False
            self._check_unresolved(db, identity)
            timestamp = now()
            db.execute(
                "INSERT INTO action_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,'{}',?,?)",
                (
                    execution_id,
                    identity.scope,
                    identity.fingerprint,
                    identity.connection_id,
                    identity.connection_identity,
                    identity.character,
                    identity.session,
                    identity.scene,
                    identity.operation,
                    identity.definition_digest,
                    identity.binding_id,
                    identity.recovery_json,
                    identity.user_id,
                    request_key or str(uuid4()),
                    ActionOutcome.DISPATCHING,
                    timestamp,
                    timestamp,
                ),
            )
            row = db.execute(
                "SELECT * FROM action_executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            assert row is not None
            return self._record(row), True

    def finish(
        self,
        execution_id: str,
        outcome: ActionOutcome,
        *,
        projection: Json | None = None,
        task_id: str | None = None,
    ) -> ActionRecord:
        """projectionは回復境界で安全化した結果だけ。引数・raw payloadを受け取らない。"""
        if outcome == ActionOutcome.DISPATCHING:
            raise ValueError("cannot restore dispatching")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM action_executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            if row is None:
                raise MCPFailure("recovery", "unknown_execution")
            current = self._record(row)
            if current.outcome not in UNRESOLVED and not (
                current.outcome == outcome == ActionOutcome.CONFLICT
                and projection is not None
            ):
                # 古い照会や遅れて返ったcancelを確定済み結果へ上書きしない。
                return current
            db.execute(
                "UPDATE action_executions SET outcome=?,projection=?,task_id=COALESCE(?,task_id),updated_at=? WHERE execution_id=?",
                (outcome, encode(projection or {}), task_id, now(), execution_id),
            )
        return self.get(execution_id)

    def request_cancel(self, execution_id: str) -> ActionRecord:
        with self.store.transaction() as db:
            db.execute(
                "UPDATE action_executions SET cancel_requested=1,updated_at=? WHERE execution_id=?",
                (now(), execution_id),
            )
        return self.get(execution_id)

    def pending(self) -> tuple[ActionRecord, ...]:
        with self.store.transaction() as db:
            return tuple(
                self._record(r)
                for r in db.execute(
                    "SELECT * FROM action_executions ORDER BY created_at"
                )
                if r["outcome"] in UNRESOLVED
            )

    def for_task(self, connection_id: str, task_id: str) -> tuple[ActionRecord, ...]:
        with self.store.transaction() as db:
            return tuple(
                self._record(r)
                for r in db.execute(
                    "SELECT * FROM action_executions WHERE connection_id=? AND task_id=?",
                    (connection_id, task_id),
                )
            )

    def detach_dispatches(self) -> None:
        """再起動時に、送信中だった操作を結果不明へ移す。再送権は作らない。"""
        with self.store.transaction() as db:
            db.execute(
                "UPDATE action_executions SET outcome=?,updated_at=? WHERE outcome=?",
                (ActionOutcome.RESULT_UNKNOWN, now(), ActionOutcome.DISPATCHING),
            )

    def scope_records(
        self, character: str, scene: ExecutionScene, scope: str
    ) -> tuple[ActionRecord, ...]:
        with self.store.transaction() as db:
            return tuple(
                self._record(r)
                for r in db.execute(
                    "SELECT * FROM action_executions WHERE character=? AND scene=? AND scope=? ORDER BY created_at",
                    (character, scene, scope),
                )
            )
