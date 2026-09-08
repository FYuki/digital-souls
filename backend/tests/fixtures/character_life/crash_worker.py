"""正本commit直後・DBOS step完了前の強制停止を検証するtest専用process。"""

import asyncio
import json
import os
from pathlib import Path
import sys

from dbos import DBOS
from app.character_life.models import Kind, LifeState, Result
from app.character_life.runtime import Runtime, Settings
from app.character_life.service import Service, ELYTH_ENDPOINT
from app.character_life.store import Store
from app.external_mcp import Connection, ExecutionGate, Registry
from app.tool_use.projection import Sanitizer
from tests.external_mcp_test_support import FakeSource, discovery, manifest
from tests.module.test_character_life import Cognition, Privacy, Scanner


async def main(root: Path):
    crash_at = sys.argv[2] if len(sys.argv) > 2 else "domain_commit"
    store = Store(root / "life.db")
    connection = Connection.from_manifest(
        manifest(
            connection_id="elyth", transport="streamable_http", endpoint=ELYTH_ENDPOINT
        )
    )
    registry = Registry()
    registry.register(connection)
    gate = ExecutionGate(registry)
    source = FakeSource(connection, discovery("get_information"))
    if not store.states("miori"):
        state = store.save_state(
            LifeState(
                character_id="miori",
                kind=Kind.GOAL_INTENTION,
                content="公開話題を探す",
                target_id="elyth",
                source="user",
            )
        )
        grant = store.set_grant("miori", "elyth", connection.identity, True)
        run = store.create_run(state, grant, "crash-test", True)
    else:
        run = store.runs("miori")[0]
    original_call = source.call_tool

    async def counted(*args, **kwargs):
        with (root / "calls").open("a") as f:
            f.write("read\n")
        return await original_call(*args, **kwargs)

    source.call_tool = counted
    original_finish = store.finish

    def crash_after_commit(*args, **kwargs):
        final = original_finish(*args, **kwargs)
        if (
            crash_at == "domain_commit"
            and final.result is Result.APPLIED
            and not (root / "crashed").exists()
        ):
            (root / "crashed").write_text("yes")
            os._exit(23)
        return final

    store.finish = crash_after_commit

    class Memory:
        async def record_observation(
            self, *, character, run_id, experienced_at, topic, source_revisions
        ):
            fixed = json.dumps(
                [character, run_id, experienced_at.isoformat(), topic, source_revisions]
            )
            record = root / "memory-input"
            if record.exists():
                assert record.read_text() == fixed
                return Result.NO_CHANGE
            record.write_text(fixed)
            (root / "crashed").write_text("yes")
            os._exit(24)

        async def catch_up(self, character):
            return Result.NO_CHANGE

    async with gate.attach("elyth", source):
        service = Service(
            store,
            gate,
            Cognition(),
            Privacy(),
            Sanitizer(Scanner()),
            foreground_busy=lambda: False,
            memory=Memory() if crash_at == "memory_commit" else None,
        )
        runtime = Runtime(service, root, Settings(True, "0 0 1 1 *"))
        await runtime.start()
        try:
            for _ in range(100):
                status = await asyncio.to_thread(
                    DBOS.get_workflow_status, f"{run.workflow_id}:1"
                )
                if status is not None and status.status == "SUCCESS":
                    break
                await asyncio.sleep(0.05)
            assert status is not None and status.status == "SUCCESS"
            if crash_at == "memory_commit":
                assert store.run(str(run.id)).dependency_results["episode"] == "NO_CHANGE"
            print(
                json.dumps(
                    {
                        "result": store.run(str(run.id)).result,
                        "shares": len(
                            [
                                s
                                for s in store.states("miori")
                                if s.kind is Kind.SHARE_CANDIDATE
                            ]
                        ),
                        "reads": len((root / "calls").read_text().splitlines()),
                        "workflow": status.status,
                    }
                )
            )
        finally:
            await runtime.close()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
