import importlib
import threading
from pathlib import Path
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
    import app.runtime.application as application_runtime
    memory_policy = patch.object(application_runtime, "resolved_memory_policy")

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

    import app.runtime.application as application_runtime

    with patch.object(
        application_runtime,
        "resolved_memory_policy",
        side_effect=ValueError("invalid memory policy"),
    ):
        with pytest.raises(ValueError, match="invalid memory policy"):
            async with main.lifespan(FastAPI()):
                pytest.fail("invalid policy must prevent startup")

    assert legacy_index.read_bytes() == b"legacy"
    assert not cutover_marker.exists()


def _core_reply_test_service(
    *,
    prompt,
    settings,
    prepared_threads: list[int],
    counter_threads: list[int] | None = None,
    life_threads: list[int] | None = None,
) -> object:
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app import _chat_runtime
    from app.prompting import CharacterPrompt

    def build_prompt(**_kwargs: object) -> object:
        prepared_threads.append(threading.get_ident())
        return prompt

    def count(messages: object) -> int:
        if counter_threads is not None:
            counter_threads.append(threading.get_ident())
        return sum(len(message.content) for message in messages)

    def life_context(character: str, built: object) -> object:
        if life_threads is not None:
            life_threads.append(threading.get_ident())
        return built

    return _chat_runtime.ChatService(
        _chat_runtime.ChatRuntimeConfig(
            rag_enabled=False,
            memory_policy=None,
            prompt_config=settings,
            chroma_path=Path("/test/runtime-data/chroma"),
        ),
        SimpleNamespace(open_session=lambda *_: SimpleNamespace()),
        _chat_runtime.ChatRuntimeDependencies(
            character_definition_loader=(
                lambda _c: _chat_runtime.CharacterRuntimeDefinition(
                    prompt=CharacterPrompt("", "", "", "", "", ""),
                    character_book=None,
                )
            ),
            prompt_builder=build_prompt,
            llm_response_generator=lambda *_a, **_k: "unused",
            input_token_counter=count,
            privacy_scanner=MagicMock(),
            semantic_classifier=MagicMock(),
            approved_memory_repository=MagicMock(),
            memory_embedder=lambda _text: [0.1],
            memory_formation_submitter=MagicMock(),
            life_context=life_context if life_threads is not None else None,
        ),
    )


@pytest.mark.anyio
async def test_core_reply_prepares_prompt_outside_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main
    from app.conversation_history.service import HistorySession
    from app.model_settings import resolve_model_settings

    from app.prompting import BuiltPrompt, PromptMessage, PromptRole, PromptUsage

    prompt = BuiltPrompt(
        messages=(PromptMessage(PromptRole.USER, "こんにちは"),),
        usage=PromptUsage(
            total=3, character=0, character_lore=0, rag=0, history=0,
            current_user=3, post_history=0, omitted_character_lore_entries=0,
            omitted_rag_items=0, omitted_history_exchanges=0,
        ),
        character_lore_decisions=(),
    )
    prepared_threads: list[int] = []
    recorded_prompts: list[object] = []
    stream_arguments: list[tuple[object, int, object]] = []

    session = cast(HistorySession, object())
    settings = resolve_model_settings({}, assistant_max_generation_tokens=123)
    chat_service = _core_reply_test_service(
        prompt=prompt, settings=settings, prepared_threads=prepared_threads,
    )
    monkeypatch.setattr(
        chat_service,
        "record_successful_prompt_references",
        recorded_prompts.append,
    )
    event_loop_thread = threading.get_ident()

    async def fake_stream_response(
        built_prompt: object, *, max_output_tokens: int, settings: object, latency_sensitive: bool
    ):
        assert latency_sensitive is True
        stream_arguments.append((built_prompt, max_output_tokens, settings))
        yield "こんにちは"

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

    assert prepared_threads
    assert all(thread != event_loop_thread for thread in prepared_threads)
    assert chunks == ["こんにちは"]
    assert stream_arguments == [(prompt, 123, settings)]
    assert recorded_prompts == [prompt]


@pytest.mark.anyio
async def test_core_reply_tool_preparation_runs_sync_io_outside_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool枠確保・結果合成・Life Stateの同期処理もevent loop外で実行する。"""
    from app import main
    from app.conversation_history.service import HistorySession
    from app.model_settings import resolve_model_settings
    from app.prompting import BuiltPrompt, PromptMessage, PromptRole, PromptUsage
    from app.tool_use.service import ToolMaterial

    prompt = BuiltPrompt(
        messages=(PromptMessage(PromptRole.USER, "質問"),),
        usage=PromptUsage(
            total=1, character=0, character_lore=0, rag=0, history=0,
            current_user=1, post_history=0, omitted_character_lore_entries=0,
            omitted_rag_items=0, omitted_history_exchanges=0,
        ),
        character_lore_decisions=(),
    )
    prepared_threads: list[int] = []
    counter_threads: list[int] = []
    life_threads: list[int] = []

    class Tools:
        async def run(
            self, *_args: object, before_execute=None, **_kwargs: object
        ) -> ToolMaterial:
            if before_execute is not None:
                await before_execute()
            return ToolMaterial(
                results=({"outcome": "succeeded", "text": "資料"},)
            )

    async def fake_stream_response(built_prompt: object, **_kwargs: object):
        yield "応答"

    session = cast(HistorySession, object())
    settings = resolve_model_settings(
        {}, chat_context_tokens=4000, assistant_max_generation_tokens=100
    )
    chat_service = _core_reply_test_service(
        prompt=prompt,
        settings=settings,
        prepared_threads=prepared_threads,
        counter_threads=counter_threads,
        life_threads=life_threads,
    )
    event_loop_thread = threading.get_ident()
    monkeypatch.setattr(main.llm_router, "stream_response", fake_stream_response)

    chunks = [
        chunk
        async for chunk in main._stream_core_reply(
            chat_service,
            settings,
            "miori",
            session,
            "質問",
            tools=Tools(),
            conversation_id="conversation",
        )
    ]

    assert chunks == ["応答"]
    sync_threads = prepared_threads + counter_threads + life_threads
    assert prepared_threads and counter_threads and life_threads
    assert all(thread != event_loop_thread for thread in sync_threads)


def test_controlled_memory_isolation_rejects_character_life(monkeypatch, runtime_paths):
    from app import main
    monkeypatch.setenv("VOICE_MEASUREMENT_DISABLE_MEMORY_FORMATION", "true")
    monkeypatch.setenv("VOICE_MEASUREMENT_KIND", "controlled_baseline")
    monkeypatch.setenv("VOICE_CONTROLLED_TRACE_PATH", str(runtime_paths.data_root / "controlled.jsonl"))
    monkeypatch.setenv("DS_CHARACTER_LIFE_ENABLED", "true")
    with pytest.raises(ValueError, match="Character Life disabled"):
        with TestClient(main.app):
            pass


def test_memory_isolation_guard_runs_before_inference_creation(monkeypatch):
    from app import main
    monkeypatch.setenv("VOICE_MEASUREMENT_DISABLE_MEMORY_FORMATION", "true")
    monkeypatch.delenv("VOICE_MEASUREMENT_KIND", raising=False)
    import app.runtime.inference as inference_runtime
    with patch.object(inference_runtime, "create_inference_runtime") as create:
        with pytest.raises(ValueError, match="controlled test"):
            with TestClient(main.app):
                pass
    create.assert_not_called()


def test_restore_rejection_preserves_controlled_memory_policy(monkeypatch, runtime_paths):
    from app import main
    from app.backup_restore.models import RestoreRecoveryRequiredError
    from app.voice_measurement_memory import POLICY_PATH

    policy = runtime_paths.data_root / POLICY_PATH
    policy.parent.mkdir(parents=True, exist_ok=True)
    evidence = b'{"previous_measurement":true}\n'
    policy.write_bytes(evidence)
    runtime_paths.restore_intent_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("VOICE_MEASUREMENT_KIND", "controlled_baseline")
    monkeypatch.setenv("VOICE_CONTROLLED_TRACE_PATH", str(runtime_paths.data_root / "controlled.jsonl"))
    with pytest.raises(RestoreRecoveryRequiredError):
        with TestClient(main.app):
            pass
    assert policy.read_bytes() == evidence
