import importlib


def test_runtime_module_setup_keeps_websocket_exception_bindings_current() -> None:
    import app.routers.ws as ws_router

    module_names = (
        "app.memory.chroma_store",
        "app.memory.conversation_log",
        "app.memory.rag_service",
        "app._chat_runtime",
        "app.chat_service",
        "app.routers.chat",
        "app.main",
    )
    modules = {
        module_name: importlib.import_module(module_name) for module_name in module_names
    }
    chat_service = modules["app.chat_service"]

    assert ws_router.CharacterNotFoundError is chat_service.CharacterNotFoundError
    assert ws_router.ChatBackendError is chat_service.ChatBackendError
    assert ws_router.ChatTimeoutError is chat_service.ChatTimeoutError
