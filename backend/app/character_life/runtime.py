"""DBOSのlifecycleを一箇所で所有する。checkpointへはIDと固定resultのみを保存する。"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future
from contextvars import Context
from dataclasses import dataclass
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import UUID
from typing import Any, TypeVar
from app.external_mcp.models import digest

from dbos import DBOS, DBOSConfig, SetEnqueueOptions, SetWorkflowID

from .models import LifeError, Result, Run, now
from .service import Service

QUEUE = "character-life"
SCHEDULE = "character-life-scan"
_owner: Runtime | None = None
T = TypeVar("T")


def owner() -> Runtime:
    if _owner is None:
        raise RuntimeError("Character Life runtime is unavailable")
    return _owner


@DBOS.step(retries_allowed=False)
def activity_step(run_id: str, attempt: int) -> str:
    return owner().execute_on_owner_loop(run_id, attempt)


@DBOS.workflow(name="character_life_activity_v1")
def activity_workflow(run_id: str, attempt: int) -> str:
    return activity_step(run_id, attempt)


@DBOS.step(retries_allowed=False)
def formation_step(character: str, workflow_id: str) -> str:
    runtime = owner()
    previous = runtime.service.store.formation_job_result(character, workflow_id)
    if previous is not None:
        return previous
    future = runtime.on_owner_loop(runtime.service.form_life_states(character))
    try:
        result = future.result(timeout=runtime.service.timeout + 10)
        runtime.service.store.finish_formation_job(
            character, workflow_id, Result(result)
        )
        return result
    finally:
        with runtime.future_lock:
            runtime.futures.discard(future)


@DBOS.workflow(name="character_life_formation_v1")
def formation_workflow(character: str, workflow_id: str) -> str:
    return formation_step(character, workflow_id)


@DBOS.step(retries_allowed=False)
def schedule_step(scheduled_at: str) -> int:
    runtime = owner()
    future = runtime.on_owner_loop(runtime.scan(scheduled_at))
    try:
        return future.result(timeout=60)
    finally:
        with runtime.future_lock:
            runtime.futures.discard(future)


@DBOS.workflow(name="character_life_schedule_v1")
def schedule_workflow(scheduled_at: datetime, context: Any) -> None:
    schedule_step(scheduled_at.isoformat())


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    cron: str = "*/30 * * * *"

    @classmethod
    def load(cls, environment: dict[str, str]) -> Settings:
        value = environment.get("DS_CHARACTER_LIFE_ENABLED", "false")
        if value not in {"true", "false"}:
            raise ValueError("invalid DS_CHARACTER_LIFE_ENABLED")
        cron = environment.get("DS_CHARACTER_LIFE_CRON", "*/30 * * * *")
        if len(cron) > 100 or len(cron.split()) not in {5, 6}:
            raise ValueError("invalid DS_CHARACTER_LIFE_CRON")
        return cls(value == "true", cron)


class Runtime:
    def __init__(
        self,
        service: Service,
        data_root: Path,
        settings: Settings,
        *,
        characters: Callable[[], tuple[str, ...]] = lambda: (),
    ) -> None:
        self.service, self.settings = service, settings
        self.characters = characters
        self.system_path = data_root / "character-life-system.sqlite"
        self.loop = asyncio.get_running_loop()
        self.futures: set[Future[Any]] = set()
        self.future_lock = Lock()
        self.started = False

    async def start(self) -> None:
        global _owner
        if _owner is not None:
            raise RuntimeError("Character Life runtime already exists")
        _owner = self
        config: DBOSConfig = {
            "name": "digital-souls-life",
            "application_version": "life-v1",
            "system_database_url": f"sqlite:///{self.system_path}",
            "enable_otlp": False,
            "run_admin_server": False,
        }
        try:
            DBOS(config=config)
            await asyncio.to_thread(DBOS.launch)
            await asyncio.to_thread(
                DBOS.register_queue, QUEUE, global_concurrency=1, priority_enabled=True
            )
            await asyncio.to_thread(
                DBOS.apply_schedules,
                [
                    {
                        "schedule_name": SCHEDULE,
                        "workflow_fn": schedule_workflow,
                        "schedule": self.settings.cron,
                        "automatic_backfill": False,
                        "context": None,
                    }
                ],
            )
            self.started = True
            # 正本への登録後・enqueue前に停止しても、安定IDで同じ実行だけを再投入する。
            for run in self.service.store.open_runs():
                await self.enqueue(run)
            for job in self.service.store.formation_jobs(None):
                await self.enqueue_formation(str(job["character_id"]), str(job["id"]))
        except BaseException:
            try:
                await self.close()
            except Exception:
                logging.getLogger(__name__).warning("Character Life runtime cleanup failed")
            raise

    def on_owner_loop(self, coroutine: Coroutine[Any, Any, T]) -> Future[T]:
        # DBOS stepのContextVarをアプリ側へ運ばない。scanが投入する活動・形成は
        # 正本の安定IDで回復する独立workflowであり、実行中stepの子ではない。
        # closeのsnapshotとの間に、投入済みだが未登録のFutureを作らない。
        with self.future_lock:
            future = Context().run(
                asyncio.run_coroutine_threadsafe, coroutine, self.loop
            )
            self.futures.add(future)
        return future

    def execute_on_owner_loop(self, run_id: str, attempt: int) -> str:
        future = self.on_owner_loop(self.service.execute(run_id, attempt=attempt))
        try:
            return future.result(timeout=self.service.timeout + 10)
        finally:
            with self.future_lock:
                self.futures.discard(future)

    async def enqueue(self, run: Run) -> None:
        def submit() -> None:
            if self.service.closing:
                raise LifeError(Result.DEFERRED, "runtime_stopping")
            with (
                SetWorkflowID(f"{run.workflow_id}:{run.attempt}"),
                SetEnqueueOptions(priority=1 if run.requested else 10),
            ):
                DBOS.enqueue_workflow(
                    QUEUE, activity_workflow, str(run.id), run.attempt
                )

        await asyncio.to_thread(submit)

    async def submit(
        self, character: str, state_id: UUID, request_id: str, *, requested: bool
    ) -> Run:
        state = self.service.store.state(character, state_id)
        if state.target_id is None:
            raise LifeError(Result.REJECTED, "target_required")
        grant = self.service.store.grant(character, state.target_id)
        run = self.service.store.create_run(state, grant, request_id, requested)
        if run.phase == "queued":
            await self.enqueue(run)
        return run

    async def scan(self, scheduled_at: str) -> int:
        # 再起動後に古いschedule workflowだけが回復しても、過去の活動を作らない。
        delay = (now() - datetime.fromisoformat(scheduled_at)).total_seconds()
        if self.service.closing or delay < 0 or delay > 120:
            return 0
        submitted = 0
        characters: set[str] = set(self.characters())
        for state in self.service.store.eligible_states():
            characters.add(state.character_id)
            try:
                assert state.target_id is not None
                grant = self.service.store.grant(state.character_id, state.target_id)
                if not grant.enabled:
                    continue
                request_id = f"schedule:{scheduled_at}:{state.id}:{state.revision}"
                await self.submit(
                    state.character_id, state.id, request_id, requested=False
                )
                submitted += 1
            except LifeError:
                continue
        for character in characters:
            try:
                await self.submit_formation(character, scheduled_at)
            except LifeError:
                continue
        return submitted

    async def submit_formation(self, character: str, request_id: str) -> str:
        workflow_id = "life-formation-" + digest([character, request_id])[7:]
        self.service.store.register_formation_job(character, workflow_id)
        await self.enqueue_formation(character, workflow_id)
        return workflow_id

    async def enqueue_formation(self, character: str, workflow_id: str) -> None:
        def submit() -> None:
            if self.service.closing:
                raise LifeError(Result.DEFERRED, "runtime_stopping")
            with SetWorkflowID(workflow_id), SetEnqueueOptions(priority=20):
                DBOS.enqueue_workflow(QUEUE, formation_workflow, character, workflow_id)

        await asyncio.to_thread(submit)

    async def resume(self, run_id: str) -> Run:
        run = self.service.store.run(run_id)
        if run.phase == "paused" or (
            run.phase == "finished" and run.result is Result.DEFERRED
        ):
            if run_id in self.service.cancellations:
                raise LifeError(Result.DEFERRED, "previous_attempt_stopping")
            # 中断された読取は新しいattempt。正本の完了結果と共有候補は再適用しない。
            run = run.model_copy(
                update={
                    "phase": "queued",
                    "result": None,
                    "reason": "resumed",
                    "finished_at": None,
                    "attempt": run.attempt + 1,
                }
            )
            self.service.store.save_run(run, expected_attempt=run.attempt - 1)
            await self.enqueue(run)
        return run

    async def close(self) -> None:
        global _owner
        await self.service.stop()
        self.started = False
        # DBOSの停止をevent loop外で待ち、進行中の非同期処理に終了機会を与える。
        if _owner is self:
            try:
                await asyncio.to_thread(
                    DBOS.destroy, workflow_completion_timeout_sec=10
                )
            finally:
                with self.future_lock:
                    futures = tuple(self.futures)
                for future in futures:
                    future.cancel()
                _owner = None
