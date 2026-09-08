"""Life State・許可・監査のSQLite正本。DBOS checkpointには参照IDだけを渡す。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

from .models import Grant, Kind, LifeError, LifeState, Result, Run, StateStatus, now


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ValueError("unsupported Character Life database version")
            db.executescript("""
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
                PRAGMA user_version=1;
            """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def states(self, character: str) -> list[LifeState]:
        with self.transaction() as db:
            return [
                LifeState.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT document FROM life_states WHERE character_id=? ORDER BY rowid DESC LIMIT 200",
                    (character,),
                )
            ]

    def state(self, character: str, state_id: UUID) -> LifeState:
        with self.transaction() as db:
            row = db.execute(
                "SELECT document FROM life_states WHERE character_id=? AND id=?",
                (character, str(state_id)),
            ).fetchone()
        if row is None:
            raise LifeError(Result.REJECTED, "state_not_found")
        return LifeState.model_validate_json(row[0])

    def state_history(self, character: str, state_id: UUID) -> list[LifeState]:
        with self.transaction() as db:
            return [
                LifeState.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT document FROM life_state_history WHERE character_id=? AND id=? ORDER BY revision",
                    (character, str(state_id)),
                )
            ]

    def audit(self, character: str, *, after: int = 0) -> list[dict[str, object]]:
        with self.transaction() as db:
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
        argument_fingerprint: str,
        outcome: str,
    ) -> None:
        # arguments・native payloadは監査へ複製せず、照合用IDと結果だけを残す。
        detail = json.dumps(
            {
                "attempt": run.attempt,
                "execution_id": execution_id,
                "candidate_id": candidate_id,
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
        with self.transaction() as db:
            return [
                Grant.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT document FROM autonomy_grants WHERE character_id=?",
                    (character,),
                )
            ]

    def grant(self, character: str, connection: str) -> Grant:
        with self.transaction() as db:
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
        with self.transaction() as db:
            row = db.execute(
                "SELECT document FROM life_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise LifeError(Result.REJECTED, "run_not_found")
        return Run.model_validate_json(row[0])

    def runs(self, character: str) -> list[Run]:
        with self.transaction() as db:
            return [
                Run.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT document FROM life_runs WHERE character_id=? ORDER BY rowid DESC LIMIT 100",
                    (character,),
                )
            ]

    def save_run(self, run: Run, *, expected_attempt: int | None = None) -> None:
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
        with self.transaction() as db:
            runs = [
                Run.model_validate_json(r[0])
                for r in db.execute("SELECT document FROM life_runs")
            ]
        return [r for r in runs if r.phase in {"queued", "running"}]

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
        with self.transaction() as db:
            states = [
                LifeState.model_validate_json(r[0])
                for r in db.execute("SELECT document FROM life_states")
            ]
        return [
            s
            for s in states
            if s.kind is Kind.GOAL_INTENTION
            and s.status is StateStatus.ACTIVE
            and s.target_id
        ]

    def invalidate_reflections(
        self, character: str, source_ids: frozenset[UUID]
    ) -> int:
        affected = 0
        for state in self.states(character):
            if state.source == "reflection" and source_ids.intersection(
                state.source_ids
            ):
                self.save_state(
                    state.model_copy(update={"status": StateStatus.DORMANT}),
                    expected_revision=state.revision,
                )
                affected += 1
        return affected

    def formation_exists(self, character: str, fingerprint: str) -> bool:
        with self.transaction() as db:
            return (
                db.execute(
                    "SELECT 1 FROM life_formations WHERE character_id=? AND fingerprint=?",
                    (character, fingerprint),
                ).fetchone()
                is not None
            )

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
