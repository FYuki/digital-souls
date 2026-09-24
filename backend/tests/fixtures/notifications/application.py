"""本番appの通知限定起動を使い、試験用の配送位置観測だけを付加する。"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from app.main import app

production_lifespan = app.router.lifespan_context


@asynccontextmanager
async def observed_lifespan(application):
    if os.environ.get("DS_ENVIRONMENT_ID") != "test":
        raise RuntimeError("test environment required")
    root = Path(os.environ["DS_NOTIFICATION_CONFORMANCE_ROOT"])
    async with production_lifespan(application):
        async def checkpoint():
            while True:
                runtime = application.state.event_source
                if runtime is not None:
                    state = runtime.store.state("fixture")
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


app.router.lifespan_context = observed_lifespan
