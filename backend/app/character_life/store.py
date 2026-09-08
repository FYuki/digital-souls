"""Life State・許可・監査のSQLite正本。DBOS checkpointには参照IDだけを渡す。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID, uuid4

from .models import Grant, Kind, LifeError, LifeState, Result, Run, StateStatus, now


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # WALはtransaction外で設定し、読取snapshotが書込commitを妨げないようにする。
        with closing(sqlite3.connect(path, timeout=5)) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2}:
                raise ValueError("unsupported Character Life database version")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS life_states (
                    id TEXT PRIMARY KEY, character_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, document TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS life_states_character ON life_states(character_id);
                CREATE TABLE IF NOT EXISTS autonomy_grants (
                    character_id TEXT NOT NULL, connection_id TEXT NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY(character_id, connection_id));
                CREATE TABLE IF NOT EXISTS life_runs (
                    id TEXT PRIMARY KEY, character_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, document TEXT NOT NULL,
                    UNIQUE(character_id, request_id));
                CREATE TABLE IF NOT EXISTS life_formations (
                    character_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    PRIMARY KEY(character_id, fingerprint));
                CREATE TABLE IF NOT EXISTS life_formation_jobs (
                    id TEXT PRIMARY KEY, character_id TEXT NOT NULL,
                    result TEXT, created_at TEXT NOT NULL, finished_at TEXT);
                CREATE TABLE IF NOT EXISTS life_state_history (
                    id TEXT NOT NULL, revision INTEGER NOT NULL,
                    character_id TEXT NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY(id, revision));
                CREATE TABLE IF NOT EXISTS life_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL, entity TEXT NOT NULL,
                    entity_id TEXT NOT NULL, event TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS life_state_insert AFTER INSERT ON life_states
                BEGIN
                    INSERT INTO life_state_history VALUES (
                        NEW.id,NEW.revision,NEW.character_id,NEW.document);
                END;
                CREATE TRIGGER IF NOT EXISTS life_state_update AFTER UPDATE ON life_states
                BEGIN
                    INSERT INTO life_state_history VALUES (
                        NEW.id,NEW.revision,NEW.character_id,NEW.document);
                END;
                CREATE TRIGGER IF NOT EXISTS life_grant_insert AFTER INSERT ON autonomy_grants
                BEGIN
                    INSERT INTO life_audit(character_id,entity,entity_id,event,detail,created_at)
                    VALUES(NEW.character_id,'grant',NEW.connection_id,'changed',
                        json_object('revision',json_extract(NEW.document,'$.revision'),
                                    'enabled',json_extract(NEW.document,'$.enabled')),
                        strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                END;
                CREATE TRIGGER IF NOT EXISTS life_run_insert AFTER INSERT ON life_runs
                BEGIN
                    INSERT INTO life_audit(character_id,entity,entity_id,event,detail,created_at)
                    VALUES(NEW.character_id,'activity',NEW.id,'created',
                        json_object('attempt',json_extract(NEW.document,'$.attempt'),
                                    'phase',json_extract(NEW.document,'$.phase')),
                        strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                END;
                CREATE TRIGGER IF NOT EXISTS life_run_update AFTER UPDATE ON life_runs
                WHEN NEW.document != OLD.document
                BEGIN
                    INSERT INTO life_audit(character_id,entity,entity_id,event,detail,created_at)
                    VALUES(NEW.character_id,'activity',NEW.id,'transition',
                        json_object('attempt',json_extract(NEW.document,'$.attempt'),
                                    'phase',json_extract(NEW.document,'$.phase'),
                                    'result',json_extract(NEW.document,'$.result'),
                                    'reason',json_extract(NEW.document,'$.reason')),
                        strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                END;
                INSERT OR IGNORE INTO life_state_history
                    SELECT id,revision,character_id,document FROM life_states;
                CREATE INDEX IF NOT EXISTS life_runs_phase
                    ON life_runs(json_extract(document,'$.phase'));
                CREATE INDEX IF NOT EXISTS life_states_eligible
                    ON life_states(json_extract(document,'$.status'),
                                   json_extract(document,'$.kind'),
                                   json_extract(document,'$.target_id'));
                UPDATE life_runs
                    SET document=json_set(document,'$.handoff.character_id',character_id)
                    WHERE json_type(document,'$.handoff')='object'
                      AND json_type(document,'$.handoff.character_id') IS NULL
                      AND (SELECT user_version FROM pragma_user_version) < 2;
                PRAGMA user_version=2;
            """)

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def states(
        self,
        character: str,
        *,
        active_only: bool = False,
        limit: int = 200,
    ) -> list[LifeState]:
        with self.transaction(write=False) as db:
            return [
                LifeState.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT document FROM life_states WHERE character_id=? "
                    "AND (?=0 OR json_extract(document,'$.status')='ACTIVE') "
                    "ORDER BY rowid DESC LIMIT ?",
                    (character, active_only, limit),
                )
            ]

    def state(self, character: str, state_id: UUID) -> LifeState:
        with self.transaction(write=False) as db:
            row = db.execute(
                "SELECT document FROM life_states WHERE character_id=? AND id=?",
                (character, str(state_id)),
            ).fetchone()
        if row is None:
            raise LifeError(Result.REJECTED, "state_not_found")
        return LifeState.model_validate_json(row[0])

    def state_history(self, character: str, state_id: UUID) -> list[LifeState]:
        with self.transaction(write=False) as db:
            return [
                LifeState.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT document FROM life_state_history WHERE character_id=? AND id=? ORDER BY revision",
                    (character, str(state_id)),
                )
            ]

    def audit(self, character: str, *, after: int = 0) -> list[dict[str, object]]:
        with self.transaction(write=False) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM life_audit WHERE character_id=? AND sequence>? ORDER BY sequence LIMIT 100",
                    (character, after),
                )
            ]

    def record_operation(
        self,
        run: Run,
        *,
        execution_id: str,
        candidate_id: str,
        operation_label: str,
        snapshot_revision: str,
        argument_fingerprint: str,
        outcome: str,
    ) -> None:
        # arguments・native payloadは監査へ複製せず、照合用IDと結果だけを残す。
        detail = json.dumps(
            {
                "attempt": run.attempt,
                "execution_id": execution_id,
                "candidate_id": candidate_id,
                "operation_label": operation_label,
                "snapshot_revision": snapshot_revision,
                "argument_fingerprint": argument_fingerprint,
                "outcome": outcome,
            }
        )
        with self.transaction() as db:
            db.execute(
                "INSERT INTO life_audit(character_id,entity,entity_id,event,detail,created_at) VALUES (?,?,?,?,?,?)",
                (
                    run.character_id,
                    "activity",
                    str(run.id),
                    "operation",
                    detail,
                    now().isoformat(),
                ),
            )

    def save_state(self, state: LifeState, *, expected_revision: int = 0) -> LifeState:
        with self.transaction() as db:
            row = db.execute(
                "SELECT character_id,revision FROM life_states WHERE id=?",
                (str(state.id),),
            ).fetchone()
            if row is None and expected_revision == 0:
                db.execute(
                    "INSERT INTO life_states VALUES (?,?,?,?)",
                    (
                        str(state.id),
                        state.character_id,
                        state.revision,
                        state.model_dump_json(),
                    ),
                )
            elif row is not None and row == (state.character_id, expected_revision):
                state = state.model_copy(
                    update={"revision": expected_revision + 1, "updated_at": now()}
                )
                db.execute(
                    "UPDATE life_states SET revision=?,document=? WHERE id=?",
                    (state.revision, state.model_dump_json(), str(state.id)),
                )
            else:
                raise LifeError(Result.CONFLICT, "state_revision_conflict")
        return state

    def grants(self, character: str) -> list[Grant]:
        with self.transaction(write=False) as db:
            return [
                Grant.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT document FROM autonomy_grants WHERE character_id=?",
                    (character,),
                )
            ]

    def grant(self, character: str, connection: str) -> Grant:
        with self.transaction(write=False) as db:
            row = db.execute(
                "SELECT document FROM autonomy_grants WHERE character_id=? AND connection_id=?",
                (character, connection),
            ).fetchone()
        if row is None:
            raise LifeError(Result.REJECTED, "autonomy_not_granted")
        return Grant.model_validate_json(row[0])

    def set_grant(
        self, character: str, connection: str, identity: str, enabled: bool
    ) -> Grant:
        with self.transaction() as db:
            row = db.execute(
                "SELECT document FROM autonomy_grants WHERE character_id=? AND connection_id=?",
                (character, connection),
            ).fetchone()
            previous = Grant.model_validate_json(row[0]) if row else None
            if (
                previous is not None
                and previous.enabled == enabled
                and previous.connection_identity == identity
            ):
                return previous
            grant = Grant(
                character_id=character,
                connection_id=connection,
                connection_identity=identity,
                enabled=enabled,
                revision=previous.revision + 1 if previous else 1,
            )
            db.execute(
                "INSERT OR REPLACE INTO autonomy_grants VALUES (?,?,?)",
                (character, connection, grant.model_dump_json()),
            )
        return grant

    def create_run(
        self, state: LifeState, grant: Grant, request_id: str, requested: bool
    ) -> Run:
        with self.transaction() as db:
            row = db.execute(
                "SELECT document FROM life_runs WHERE character_id=? AND request_id=?",
                (state.character_id, request_id),
            ).fetchone()
            if row:
                previous = Run.model_validate_json(row[0])
                if previous.state_id != state.id or previous.requested != requested:
                    raise LifeError(Result.CONFLICT, "request_id_conflict")
                return previous
            pending = [
                Run.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT document FROM life_runs WHERE character_id=? "
                    "AND json_extract(document,'$.phase') IN ('queued','running')",
                    (state.character_id,),
                )
            ]
            if len(pending) >= 100:
                raise LifeError(Result.DEFERRED, "activity_queue_full")
            if not requested and any(r.state_id == state.id for r in pending):
                raise LifeError(Result.DEFERRED, "activity_already_pending")
            run_id = uuid4()
            run = Run(
                id=run_id,
                character_id=state.character_id,
                state_id=state.id,
                state_revision=state.revision,
                grant_revision=grant.revision,
                request_id=request_id,
                requested=requested,
                workflow_id=f"life-{run_id}",
            )
            db.execute(
                "INSERT INTO life_runs VALUES (?,?,?,?)",
                (str(run.id), run.character_id, run.request_id, run.model_dump_json()),
            )
            return run

    def run(self, run_id: str) -> Run:
        with self.transaction(write=False) as db:
            row = db.execute(
                "SELECT document FROM life_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise LifeError(Result.REJECTED, "run_not_found")
        return Run.model_validate_json(row[0])

    def runs(self, character: str) -> list[Run]:
        with self.transaction(write=False) as db:
            return [
                Run.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT document FROM life_runs WHERE character_id=? ORDER BY rowid DESC LIMIT 100",
                    (character,),
                )
            ]

    def save_run(self, run: Run, *, expected_attempt: int | None = None) -> None:
        # model_copyは検証を省略するため、永続化境界でhandoffの所有者も含めて再検証する。
        run = Run.model_validate(run.model_dump())
        with self.transaction() as db:
            row = db.execute(
                "SELECT document FROM life_runs WHERE id=? AND character_id=?",
                (str(run.id), run.character_id),
            ).fetchone()
            expected = run.attempt if expected_attempt is None else expected_attempt
            if row is None or Run.model_validate_json(row[0]).attempt != expected:
                raise LifeError(Result.CONFLICT, "attempt_superseded")
            db.execute(
                "UPDATE life_runs SET document=? WHERE id=? AND character_id=?",
                (run.model_dump_json(), str(run.id), run.character_id),
            )

    def open_runs(self) -> list[Run]:
        with self.transaction(write=False) as db:
            return [
                Run.model_validate_json(r[0])
                for r in db.execute(
                    "SELECT document FROM life_runs "
                    "WHERE json_extract(document,'$.phase') IN ('queued','running')"
                )
            ]

    def finish(
        self,
        run: Run,
        result: Result,
        reason: str,
        *,
        summary: str = "",
        sources: tuple[str, ...] = (),
        dependencies: dict[str, str] | None = None,
    ) -> Run:
        """確定した共有候補と実行完了を一つのtransactionへ置き、checkpoint再開でも二重反映しない。"""
        with self.transaction() as db:
            current = Run.model_validate_json(
                db.execute(
                    "SELECT document FROM life_runs WHERE id=?", (str(run.id),)
                ).fetchone()[0]
            )
            if current.character_id != run.character_id:
                raise LifeError(Result.REJECTED, "run_owner_mismatch")
            if current.attempt != run.attempt or current.phase == "finished":
                return current
            # 停止中に返ったLLM/外部結果は採用しない。
            if current.phase == "paused":
                return current
            if summary:
                # 最後のawait以降の取消し・意図変更と正本更新を同じtransactionで直列化する。
                source = LifeState.model_validate_json(
                    db.execute(
                        "SELECT document FROM life_states WHERE id=?",
                        (str(run.state_id),),
                    ).fetchone()[0]
                )
                grant_row = db.execute(
                    "SELECT document FROM autonomy_grants WHERE character_id=? AND connection_id=?",
                    (run.character_id, source.target_id),
                ).fetchone()
                grant = Grant.model_validate_json(grant_row[0]) if grant_row else None
                if (
                    source.revision != run.state_revision
                    or source.status is not StateStatus.ACTIVE
                ):
                    result, reason, summary = Result.SUPERSEDED, "intention_changed", ""
                elif (
                    grant is None
                    or not grant.enabled
                    or grant.revision != run.grant_revision
                ):
                    result, reason, summary = (
                        Result.REJECTED,
                        "autonomy_grant_changed",
                        "",
                    )
            if summary:
                state = LifeState(
                    character_id=run.character_id,
                    kind=Kind.SHARE_CANDIDATE,
                    content=summary,
                    source="activity",
                    source_ids=(run.id,),
                    target_id=source.target_id,
                )
                db.execute(
                    "INSERT INTO life_states VALUES (?,?,?,?)",
                    (
                        str(state.id),
                        state.character_id,
                        state.revision,
                        state.model_dump_json(),
                    ),
                )
            final = current.model_copy(
                update={
                    "phase": "finished",
                    "result": result,
                    "reason": reason,
                    "finished_at": now(),
                    "source_revisions": sources,
                    "dependency_results": dependencies or {},
                }
            )
            db.execute(
                "UPDATE life_runs SET document=? WHERE id=?",
                (final.model_dump_json(), str(run.id)),
            )
            return final

    def eligible_states(self) -> list[LifeState]:
        with self.transaction(write=False) as db:
            return [
                LifeState.model_validate_json(r[0])
                for r in db.execute(
                    "SELECT document FROM life_states WHERE "
                    "json_extract(document,'$.status')='ACTIVE' AND "
                    "json_extract(document,'$.kind')='GOAL_INTENTION' AND "
                    "json_extract(document,'$.target_id') IS NOT NULL"
                )
            ]

    def invalidate_reflections(
        self, character: str, source_ids: frozenset[UUID]
    ) -> int:
        return self._invalidate_reflections(
            character, lambda state: bool(source_ids.intersection(state.source_ids))
        )

    def reconcile_reflections(self, character: str, revisions: dict[UUID, str]) -> int:
        return self._invalidate_reflections(
            character,
            lambda state: any(
                source_id not in revisions
                or state.reflection_revisions.get(source_id) != revisions[source_id]
                for source_id in state.source_ids
            ),
        )

    def _invalidate_reflections(
        self, character: str, invalid: Callable[[LifeState], bool]
    ) -> int:
        # 正本照合・全件の休眠化・履歴更新を同じtransactionで確定する。
        affected = 0
        with self.transaction() as db:
            rows = db.execute(
                "SELECT document FROM life_states WHERE character_id=? "
                "AND json_extract(document,'$.status')='ACTIVE' "
                "AND json_extract(document,'$.source')='reflection'",
                (character,),
            ).fetchall()
            for row in rows:
                state = LifeState.model_validate_json(row[0])
                if not invalid(state):
                    continue
                updated = state.model_copy(update={
                    "status": StateStatus.DORMANT,
                    "revision": state.revision + 1,
                    "updated_at": now(),
                })
                changed = db.execute(
                    "UPDATE life_states SET revision=?,document=? "
                    "WHERE id=? AND character_id=? AND revision=?",
                    (updated.revision, updated.model_dump_json(), str(state.id),
                     character, state.revision),
                )
                if changed.rowcount != 1:
                    raise LifeError(Result.CONFLICT, "state_revision_conflict")
                affected += 1
        return affected

    def formation_exists(self, character: str, fingerprint: str) -> bool:
        with self.transaction(write=False) as db:
            return (
                db.execute(
                    "SELECT 1 FROM life_formations WHERE character_id=? AND fingerprint=?",
                    (character, fingerprint),
                ).fetchone()
                is not None
            )

    def register_formation_job(self, character: str, workflow_id: str) -> None:
        with self.transaction() as db:
            if db.execute(
                "SELECT 1 FROM life_formation_jobs WHERE id=?", (workflow_id,)
            ).fetchone():
                return
            if db.execute(
                "SELECT 1 FROM life_formation_jobs WHERE character_id=? AND result IS NULL",
                (character,),
            ).fetchone():
                raise LifeError(Result.DEFERRED, "formation_already_pending")
            db.execute(
                "INSERT OR IGNORE INTO life_formation_jobs VALUES (?,?,NULL,?,NULL)",
                (workflow_id, character, now().isoformat()),
            )

    def formation_job_result(self, character: str, workflow_id: str) -> Result | None:
        with self.transaction(write=False) as db:
            row = db.execute(
                "SELECT result FROM life_formation_jobs WHERE id=? AND character_id=?",
                (workflow_id, character),
            ).fetchone()
        if row is None:
            raise LifeError(Result.REJECTED, "formation_job_not_found")
        return Result(row[0]) if row[0] else None

    def finish_formation_job(
        self, character: str, workflow_id: str, result: Result
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE life_formation_jobs SET result=?,finished_at=? WHERE id=? AND character_id=? AND result IS NULL",
                (result, now().isoformat(), workflow_id, character),
            )

    def formation_jobs(self, character: str | None) -> list[dict[str, object]]:
        with self.transaction(write=False) as db:
            db.row_factory = sqlite3.Row
            if character is None:
                rows = db.execute(
                    "SELECT * FROM life_formation_jobs WHERE result IS NULL"
                )
            else:
                rows = db.execute(
                    "SELECT * FROM life_formation_jobs WHERE character_id=? ORDER BY rowid DESC LIMIT 100",
                    (character,),
                )
            return [dict(row) for row in rows]

    def apply_formation(
        self, character: str, fingerprint: str, states: tuple[LifeState, ...]
    ) -> Result:
        with self.transaction() as db:
            if db.execute(
                "SELECT 1 FROM life_formations WHERE character_id=? AND fingerprint=?",
                (character, fingerprint),
            ).fetchone():
                return Result.NO_CHANGE
            for state in states:
                if state.character_id != character or state.source != "reflection":
                    raise LifeError(Result.REJECTED, "formation_source_invalid")
                db.execute(
                    "INSERT INTO life_states VALUES (?,?,?,?)",
                    (
                        str(state.id),
                        state.character_id,
                        state.revision,
                        state.model_dump_json(),
                    ),
                )
            db.execute(
                "INSERT INTO life_formations VALUES (?,?)", (character, fingerprint)
            )
        return Result.APPLIED if states else Result.NO_CHANGE
