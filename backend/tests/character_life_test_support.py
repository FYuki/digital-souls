"""検証で共有する最小の実行環境・合成応答。テスト収集に依存しない。"""

from contextlib import asynccontextmanager
from app.character_life.models import Kind, LifeState
from app.character_life.service import Service, ELYTH_ENDPOINT
from app.character_life.store import Store
from app.external_mcp import Connection, ExecutionGate, Registry
from app.privacy.contracts import ScanSuccess
from app.tool_use.projection import Sanitizer
from tests.external_mcp_test_support import FakeSource, discovery, manifest


class Scanner:
    def scan(self, text):
        return ScanSuccess(())


class Privacy:
    def __init__(self):
        self.allowed_value = True
        self.before_return = None

    async def allowed(self, text):
        if self.before_return:
            await self.before_return()
        return self.allowed_value


class Cognition:
    def __init__(self):
        self.contexts = []

    async def decide(self, context, cancellation):
        self.contexts.append(context)
        if context["results"]:
            return {
                "action": "finish",
                "candidate_id": "",
                "arguments_json": "",
                "summary": "色彩についての公開話題を見つけた",
            }
        return {
            "action": "call",
            "candidate_id": context["candidates"][0]["id"],
            "arguments_json": '{"value":1}',
            "summary": "",
        }


@asynccontextmanager
async def environment(
    tmp_path,
    *,
    trusted=False,
    names=("get_information",),
    busy=False,
    endpoint=ELYTH_ENDPOINT,
):
    connection = Connection.from_manifest(
        manifest(
            trusted=trusted,
            connection_id="elyth",
            transport="streamable_http",
            endpoint=endpoint,
        )
    )
    registry = Registry()
    registry.register(connection)
    gate = ExecutionGate(registry)
    source = FakeSource(connection, discovery(*names))
    store = Store(tmp_path / "life.db")
    cognition, privacy = Cognition(), Privacy()
    service = Service(
        store,
        gate,
        cognition,
        privacy,
        Sanitizer(Scanner()),
        foreground_busy=lambda: busy,
    )
    state = store.save_state(
        LifeState(
            character_id="miori",
            kind=Kind.GOAL_INTENTION,
            content="色彩の話題を探す",
            target_id="elyth",
            source="user",
        )
    )
    grant = store.set_grant("miori", "elyth", connection.identity, True)
    run = store.create_run(state, grant, "request", True)
    async with gate.attach("elyth", source):
        yield service, source, run

