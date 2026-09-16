"""通知のブラウザ適合試験用host。通知・Gate・接続管理は本物、LLMは構成しない。"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from app.characters.catalog import CharacterCatalog
from app.memory.memory_policy import resolved_memory_policy
from app.privacy.scanner import create_privacy_scanner
from app.routers.notifications import router
from app.routers.addon_admin import router as addon_router
from app.tool_use.runtime import ToolRuntime, ToolSettings


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("DS_ENVIRONMENT_ID") != "test":
        raise RuntimeError("test environment required")
    root = Path(os.environ["DS_NOTIFICATION_CONFORMANCE_ROOT"])
    catalog = CharacterCatalog(Path(__file__).resolve().parents[4] / "characters")
    runtime = ToolRuntime(ToolSettings.load(os.environ["DS_MCP_CONFIG"]), None,
                          create_privacy_scanner(resolved_memory_policy().privacy),
                          settings_path=root / "data" / "addon-settings.json",
                          character_exists=lambda c: any(entry.character_id == c for entry in catalog.scan()))
    app.state.notifications = runtime.notifications
    app.state.addon_manager = runtime.management
    await runtime.start()
    async def checkpoint():
        while True:
            if runtime.events is not None:
                state = runtime.events.store.state("fixture")
                temporary = root / "checkpoint-next.json"
                temporary.write_text(json.dumps({"position": state["position"], "status": state["status"]}))
                temporary.replace(root / "checkpoint.json")
            await asyncio.sleep(.2)
    monitor = asyncio.create_task(checkpoint())
    try:
        yield
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await runtime.close()


app = FastAPI(lifespan=lifespan)
app.include_router(router)
app.include_router(addon_router)
