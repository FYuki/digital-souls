"""実socketを使うCharacter Life API受入用server。終了時に必ずlistenerを閉じる。"""

import asyncio
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI
from httpx import AsyncClient

from app.routers.character_life import router


@asynccontextmanager
async def life_http(runtime):
    app = FastAPI()
    app.include_router(router)
    app.state.character_life_runtime = runtime
    app.state.screen_http_security = SimpleNamespace(
        allowed_origin="http://localhost:5173"
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, lifespan="off", log_level="error", access_log=False)
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("HTTP server failed to start")
                await asyncio.sleep(0.01)
        async with AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Origin": "http://localhost:5173"},
            timeout=30,
        ) as client:
            yield client
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            listener.close()
