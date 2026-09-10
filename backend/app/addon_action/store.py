"""承認状態のSQLite正本。単回許可はtransaction内で消費し、場面間で共有しない。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .models import (
    ApprovalChoice,
    ApprovalKey,
    ExecutionScene,
    OperationGroup,
    Permission,
)


@dataclass(frozen=True)
class PermissionState:
    permission: Permission
    remaining: int = 0

    @property
    def allowed(self) -> bool:
        return self.permission == Permission.ALWAYS or self.remaining > 0


class ActionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS action_permissions (
                connection_id TEXT NOT NULL, identity TEXT NOT NULL,
                operation_group TEXT NOT NULL CHECK(operation_group IN ('normal','high_impact')),
                scene TEXT NOT NULL CHECK(scene IN ('conversation','autonomous')),
                permission TEXT NOT NULL CHECK(permission IN ('unapproved','always','denied')),
                remaining INTEGER NOT NULL DEFAULT 0 CHECK(remaining >= 0),
                PRIMARY KEY(connection_id, identity, operation_group, scene)
            )""")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _state(db: sqlite3.Connection, key: ApprovalKey) -> PermissionState:
        row = db.execute(
            """SELECT permission, remaining FROM action_permissions
            WHERE connection_id=? AND identity=? AND operation_group=? AND scene=?""",
            key.values(),
        ).fetchone()
        if row is None:
            return PermissionState(
                Permission.ALWAYS
                if key.group == OperationGroup.NORMAL
                else Permission.UNAPPROVED
            )
        return PermissionState(Permission(row["permission"]), row["remaining"])

    def state(self, key: ApprovalKey) -> PermissionState:
        with self.transaction() as db:
            return self._state(db, key)

    @staticmethod
    def _write(
        db: sqlite3.Connection, key: ApprovalKey, state: PermissionState
    ) -> None:
        db.execute(
            """INSERT INTO action_permissions VALUES (?,?,?,?,?,?)
            ON CONFLICT(connection_id,identity,operation_group,scene)
            DO UPDATE SET permission=excluded.permission, remaining=excluded.remaining""",
            (*key.values(), state.permission, state.remaining),
        )

    def _choose(
        self, db: sqlite3.Connection, key: ApprovalKey, choice: ApprovalChoice
    ) -> None:
        if choice == ApprovalChoice.REJECT:
            if key.scene == ExecutionScene.AUTONOMOUS:
                self._write(db, key, PermissionState(Permission.DENIED))
            return
        state = self._state(db, key)
        self._write(
            db,
            key,
            PermissionState(Permission.ALWAYS)
            if choice == ApprovalChoice.ALWAYS
            else PermissionState(Permission.UNAPPROVED, state.remaining + 1),
        )

    def choose(self, key: ApprovalKey, choice: ApprovalChoice) -> None:
        """管理境界用。キューの回答は後続の要求IDによる重複排除を経由する。"""
        with self.transaction() as db:
            self._choose(db, key, choice)

    def consume(self, key: ApprovalKey) -> bool:
        with self.transaction() as db:
            state = self._state(db, key)
            if state.permission == Permission.ALWAYS:
                return True
            if state.permission == Permission.DENIED or state.remaining == 0:
                return False
            self._write(
                db, key, PermissionState(Permission.UNAPPROVED, state.remaining - 1)
            )
            return True
