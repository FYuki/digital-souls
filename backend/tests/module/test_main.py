import importlib
import threading
from typing import cast
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_load_dotenv_is_called_when_app_main_is_imported():
    with patch("dotenv.load_dotenv") as mock_load:
        import app.main as main_module

        importlib.reload(main_module)

    mock_load.assert_called()


def test_startup_exposes_one_transport_independent_rag_admission_service(client):
    service = client.app.state.rag_admission_service

    assert callable(service.admit)


@pytest.mark.parametrize("legacy_path_kind", ["directory", "file"])
def test_startup_removes_the_rebuildable_legacy_chroma_index(
    runtime_paths,
    legacy_path_kind: str,
):
    from app.main import app

    if legacy_path_kind == "directory":
        runtime_paths.chroma_path.mkdir(parents=True)
        (runtime_paths.chroma_path / "legacy-index.bin").write_bytes(b"legacy")
    else:
        runtime_paths.chroma_path.write_bytes(b"legacy")

    with TestClient(app):
        pass

    assert not runtime_paths.chroma_path.exists()


def test_restart_preserves_the_rebuilt_chroma_index(runtime_paths):
    from app.main import app

    with TestClient(app):
        pass

    runtime_paths.chroma_path.mkdir(parents=True)
    rebuilt_index = runtime_paths.chroma_path / "rebuilt-index.bin"
    rebuilt_index.write_bytes(b"rebuilt")

    with TestClient(app):
        pass

    assert rebuilt_index.read_bytes() == b"rebuilt"


@pytest.mark.anyio
async def test_should_validate_model_settings_before_startup_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    monkeypatch.setenv("OLLAMA_CONTEXT_TOKENS", "1024")
    memory_policy = patch.object(main, "resolved_memory_policy")

    with memory_policy as resolve_policy:
        with pytest.raises(ValueError) as exc_info:
            async with main.lifespan(FastAPI()):
                pytest.fail("invalid settings must prevent startup")

    message = str(exc_info.value)
    assert "OLLAMA_CONTEXT_TOKENS" in message
    assert "legacy inference setting" in message
    resolve_policy.assert_not_called()


@pytest.mark.anyio
async def test_startup_rejects_invalid_memory_occurred_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    monkeypatch.setenv("MEMORY_OCCURRED_TIMEZONE", "Not/A_Timezone")

    with pytest.raises(ValueError, match="MEMORY_OCCURRED_TIMEZONE"):
        async with main.lifespan(FastAPI()):
            pytest.fail("invalid timezone must prevent startup")


@pytest.mark.anyio
async def test_invalid_memory_policy_preserves_legacy_chroma_index(
    runtime_paths,
) -> None:
    from app import main

    runtime_paths.chroma_path.mkdir(parents=True)
    legacy_index = runtime_paths.chroma_path / "legacy-index.bin"
    legacy_index.write_bytes(b"legacy")
    cutover_marker = runtime_paths.data_root / ".legacy-chroma-index-removed"

    with patch.object(
        main,
        "resolved_memory_policy",
        side_effect=ValueError("invalid memory policy"),
    ):
        with pytest.raises(ValueError, match="invalid memory policy"):
            async with main.lifespan(FastAPI()):
                pytest.fail("invalid policy must prevent startup")

    assert legacy_index.read_bytes() == b"legacy"
    assert not cutover_marker.exists()


@pytest.mark.anyio
async def test_core_reply_prepares_prompt_outside_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import _chat_runtime, main
    from app.conversation_history.service import HistorySession
    from app.model_settings import resolve_model_settings

    prompt = object()
    prepared_thread: int | None = None
    recorded_prompts: list[object] = []
    stream_arguments: list[tuple[object, int, object]] = []

    class StubChatService:
        def prepare_unrecorded_generation(
            self, character: str, history_session: object, transcript: str
        ) -> tuple[object, int]:
            nonlocal prepared_thread
            prepared_thread = threading.get_ident()
            assert character == "miori"
            assert history_session is session
            assert transcript == "こんにちは"
            return prompt, 123

        def record_successful_prompt_references(self, built_prompt: object) -> None:
            recorded_prompts.append(built_prompt)

    async def fake_stream_response(
        built_prompt: object, *, max_output_tokens: int, settings: object
    ):
        stream_arguments.append((built_prompt, max_output_tokens, settings))
        yield "こんにちは"

    session = cast(HistorySession, object())
    settings = resolve_model_settings({})
    chat_service = cast(_chat_runtime.ChatService, StubChatService())
    event_loop_thread = threading.get_ident()
    monkeypatch.setattr(main.llm_router, "stream_response", fake_stream_response)

    chunks = [
        chunk
        async for chunk in main._stream_core_reply(
            chat_service,
            settings,
            "miori",
            session,
            "こんにちは",
        )
    ]

    assert prepared_thread is not None
    assert prepared_thread != event_loop_thread
    assert chunks == ["こんにちは"]
    assert stream_arguments == [(prompt, 123, settings)]
    assert recorded_prompts == [prompt]
