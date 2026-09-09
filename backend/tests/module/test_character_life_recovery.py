"""owner消失後にDBOSだけが終了した活動・形成を、実再起動で回復する。"""

import asyncio

import pytest
from dbos import DBOS

from app.character_life import runtime as module
from app.character_life.models import Result
from app.character_life.runtime import Runtime, Settings
from tests.module.test_character_life import environment


@pytest.mark.parametrize("legacy_error", [False, True])
def test_terminal_workflow_with_open_run_recovers_new_attempt(tmp_path, monkeypatch, legacy_error):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            service.pause(str(run.id))
            runtime = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            await runtime.start()
            try:
                with monkeypatch.context() as patch:
                    if legacy_error:
                        def missing_owner(*args):
                            raise RuntimeError("Character Life runtime is unavailable")
                        patch.setattr(runtime, "execute_on_owner_loop", missing_owner)
                    else:
                        patch.setattr(module, "_owner", None)
                    service.store.save_run(run)
                    await runtime.enqueue(run)
                    handle = await asyncio.to_thread(
                        DBOS.retrieve_workflow, f"{run.workflow_id}:{run.attempt}"
                    )
                    if legacy_error:
                        with pytest.raises(RuntimeError, match="unavailable"):
                            await asyncio.wait_for(asyncio.to_thread(handle.get_result), 10)
                    else:
                        assert await asyncio.wait_for(
                            asyncio.to_thread(handle.get_result), 10
                        ) == Result.DEFERRED
                assert service.store.run(str(run.id)).phase == "queued"
                assert not source.calls
            finally:
                await runtime.close()
            service.closing = False
            restarted = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            await restarted.start()
            try:
                async with asyncio.timeout(10):
                    while service.store.run(str(run.id)).phase != "finished":
                        await asyncio.sleep(0.01)
                saved = service.store.run(str(run.id))
                assert saved.result == Result.APPLIED
                assert saved.attempt == run.attempt + 1
                assert len(source.calls) == 1
                handle = await asyncio.to_thread(
                    DBOS.retrieve_workflow, f"{run.workflow_id}:{saved.attempt}"
                )
                assert await asyncio.wait_for(
                    asyncio.to_thread(handle.get_result), 10
                ) == Result.APPLIED
            finally:
                await restarted.close()
    asyncio.run(scenario())


def test_ownerless_formation_does_not_block_future_jobs_after_restart(tmp_path, monkeypatch):
    async def scenario():
        async with environment(tmp_path) as (service, _, run):
            service.store.finish(run, Result.NO_CHANGE, "setup")

            class Formation:
                calls = 0

                async def run(self, *args, **kwargs):
                    self.calls += 1
                    return Result.NO_CHANGE

            formation = Formation()
            service.formation = formation
            runtime = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            await runtime.start()
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(module, "_owner", None)
                    job = await runtime.submit_formation("miori", "lost-owner")
                    handle = await asyncio.to_thread(DBOS.retrieve_workflow, job)
                    assert await asyncio.wait_for(
                        asyncio.to_thread(handle.get_result), 10
                    ) == Result.DEFERRED
                assert service.store.formation_job_result("miori", job) is None
                assert formation.calls == 0
            finally:
                await runtime.close()
            service.closing = False
            restarted = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            await restarted.start()
            try:
                assert service.store.formation_job_result("miori", job) == Result.DEFERRED
                new_job = await restarted.submit_formation("miori", "next-formation")
                handle = await asyncio.to_thread(DBOS.retrieve_workflow, new_job)
                assert await asyncio.wait_for(
                    asyncio.to_thread(handle.get_result), 10
                ) == Result.NO_CHANGE
                assert formation.calls == 1
            finally:
                await restarted.close()
    asyncio.run(scenario())
