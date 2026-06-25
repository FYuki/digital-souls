from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from fastapi import FastAPI

from app import _chat_runtime
from app.routers.chat import router as chat_router
from app.routers.ws import router as ws_router

load_dotenv()

RAG_MEMORY_WORKERS = _chat_runtime.DEFAULT_RAG_MEMORY_WORKERS


def _app_chat_service(app: FastAPI) -> _chat_runtime.ChatService:
    return app.state.chat_service


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    executor = ThreadPoolExecutor(
        max_workers=RAG_MEMORY_WORKERS,
        thread_name_prefix=_chat_runtime.RAG_MEMORY_THREAD_PREFIX,
    )
    memory_task_queue = None
    chat_service_resolver = None
    resolver_registered = False
    chat_service_state_set = False
    try:
        memory_task_queue = _chat_runtime.create_thread_pool_memory_task_queue(executor)
        app_chat_service = _chat_runtime.create_chat_service(
            _chat_runtime.resolve_chat_runtime_config(),
            memory_task_queue,
        )
        app.state.chat_service = app_chat_service
        chat_service_state_set = True
        chat_service_resolver = lambda: _app_chat_service(app)
        _chat_runtime.register_default_chat_service_resolver(chat_service_resolver)
        resolver_registered = True
        yield
    finally:
        if resolver_registered and chat_service_resolver is not None:
            _chat_runtime.clear_default_chat_service_resolver(chat_service_resolver)
        if chat_service_state_set:
            del app.state.chat_service
        if memory_task_queue is not None:
            memory_task_queue.shutdown()
        else:
            executor.shutdown(wait=True)


app = FastAPI(lifespan=lifespan)

app.include_router(chat_router)
app.include_router(ws_router)


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
