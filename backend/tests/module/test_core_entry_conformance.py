"""3入口(HTTP /chat, Session Text, Speech)の共通Core適合を照合する試験。

同一fixtureを実入口から通し、実物 `ChatService.prepare_core_reply` を経由して
fake Providerへ届くprompt・状態遷移・保存/形成予約/Tool dispatch を観測する。
Session Text/Speechは本番 ProductionConversationCoreSessionFactory と
`main._stream_core_reply` 配線をそのまま使い、fakeは Provider/Router境界と
STT・音声合成だけに限定する。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.conversation_core.models import (
    CoreEvent,
    ResponseState,
)
from app.conversation_history.schema import initialize_conversation_history_schema
from app.conversation_history.service import (
    ConversationHistoryService,
    HistorySession,
)
from app.core_invocation import CoreInvocation
from app.llm import router as llm_router
from app.livekit_transport.core_factory import (
    ProductionConversationCoreSessionFactory,
)
from app.main import _stream_core_reply
from app.model_settings import ModelSettings
from app.privacy.history_sanitizer import create_history_sanitizer
from app.privacy.scanner import create_privacy_scanner
from app.memory.memory_policy import resolved_memory_policy
from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import resolve_runtime_paths
from app.screen_perception.provenance import ScreenLineage
from app.tool_use.routing import ToolDecision
from tests.character_card_test_support import (
    use_character_repo_root,
    write_character_card,
)
from tests.conversation_history_test_support import (
    create_repository,
)
from tests.core_entry_conformance_fixture import (
    CHARACTER_ID,
    CHARACTER_NAME,
    CHARACTER_SYSTEM_PROMPT,
    CONVERSATION_ID,
    LLM_REPLY,
    MEMORY_CONTENT,
    MEMORY_HEADING,
    RESTORED_ASSISTANT,
    RESTORED_USER,
    TEXT_INPUT_ID,
    USER_TEXT,
    CapturingOllamaAdapter,
    FixtureDelivery,
    FixtureSpeakerSynthesizer,
    FixtureSyncTranscriber,
    RecordingFormationScheduler,
    build_fake_router,
    fetch_turns,
    fixture_character_document,
    fixture_character_prompt,
    fixture_retrieval_outcome,
    insert_completed_turn,
    make_formation_observer,
    turn_status,
)
from tests.tool_use_test_support import Decisions, call, runtime as tool_runtime


pytestmark = pytest.mark.usefixtures("existing_chat_conversations")

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """asyncio.run wrapper for sync test functions."""
    return asyncio.run(coro)


def _patch_formation_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> RecordingFormationScheduler:
    """app.runtime.memory.MemoryFormationScheduler を差し替える。"""
    import app.runtime.memory as memory_runtime

    scheduler = RecordingFormationScheduler()
    monkeypatch.setattr(
        memory_runtime,
        "MemoryFormationScheduler",
        lambda *args, **kwargs: scheduler,
        raising=False,
    )
    return scheduler


def _save_fixture_memory(runtime_paths) -> str:
    """fixture memory を persona memory DB に保存し、実 memory_id を返す。"""
    from dataclasses import replace
    from uuid import uuid4
    from app.memory.persistence.approved_repository import ApprovedMemoryRepository
    from app.memory.persistence.schema import initialize_persona_memory_schema
    from tests.unit.test_approved_memory_repository import _candidate, _context

    initialize_persona_memory_schema(runtime_paths, Path.cwd())
    repository = ApprovedMemoryRepository(
        database_path=runtime_paths.persona_memory_sqlite_path,
        clock=lambda: datetime.now(UTC),
        uuid_factory=uuid4,
        outbox_uuid_factory=uuid4,
    )
    saved = repository.save(
        character_id=CHARACTER_ID,
        candidate=_candidate(MEMORY_CONTENT),
        context=replace(_context(), idempotency_key=str(uuid4())),
    )
    return str(saved.id)


def _fixture_retrieval_outcome_with_id(memory_id: str):
    """実DB保存済み memory_id を持つ RetrievalOutcome を返す。"""
    return fixture_retrieval_outcome(memory_id=memory_id)


def _fake_router_context(adapter: CapturingOllamaAdapter):
    """fake Provider router を登録・解除する context manager。"""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        router = build_fake_router(adapter)
        llm_router.register_inference_router(router)
        try:
            yield router
        finally:
            llm_router.clear_inference_router(router)

    return _ctx()


def _patch_memory_and_card(monkeypatch: pytest.MonkeyPatch, runtime_paths=None):
    """HTTP入口で使う card / memory / resolved_memory_policy を差し替える。

    memory注入は fake するが、provenance recorder が memory_id の存在確認を
    行うため、実DBに対応するレコードを挿入する必要がある。
    runtime_paths を渡すと fixture memory を実DBに保存する。
    """
    import app.memory.memory_policy as memory_policy_module

    card = MagicMock()
    card.data.character_book = None
    card.data.name = CHARACTER_NAME
    card.to_character_prompt.return_value = fixture_character_prompt()
    monkeypatch.setattr("app.main.load_character_card", lambda _char: card)

    if runtime_paths is not None:
        memory_id = _save_fixture_memory(runtime_paths)
        outcome = _fixture_retrieval_outcome_with_id(memory_id)
    else:
        outcome = fixture_retrieval_outcome()

    monkeypatch.setattr(
        "app._chat_runtime._rag_service.retrieve_prompt_memories",
        lambda *args, **kwargs: outcome,
    )
    monkeypatch.setattr(
        "app.runtime.application.resolved_memory_policy",
        lambda: MagicMock(
            privacy=memory_policy_module.resolved_memory_policy().privacy,
        ),
    )
    # RAG_ENABLED=true でmemory注入を有効化する
    monkeypatch.setenv("RAG_ENABLED", "true")


def _fixture_chat_service(
    *,
    history_service: ConversationHistoryService,
    scheduler: RecordingFormationScheduler,
    tools,
    memory_outcome,
    chroma_path,
):
    """Session入口が使う実 ChatService を本番と同じ依存で組み立てる。

    fake は Provider/Router 境界と Card loader、memory検索結果だけに限定し、
    prompt構築・Tool統合・履歴連携は製品コードをそのまま通す。
    """
    import app._chat_runtime
    from app.chat_prompt import build_chat_prompt

    model_settings = _fixture_model_settings()
    return app._chat_runtime.create_chat_service(
        app._chat_runtime.ChatRuntimeConfig(
            rag_enabled=memory_outcome is not None,
            memory_policy=(
                resolved_memory_policy() if memory_outcome is not None else None
            ),
            prompt_config=model_settings,
            chroma_path=chroma_path,
        ),
        history_service,
        app._chat_runtime.ChatRuntimeDependencies(
            character_definition_loader=_load_fixture_character,
            prompt_builder=build_chat_prompt,
            llm_response_generator=_unused_llm_generator,
            input_token_counter=_fixture_token_counter,
            privacy_scanner=create_privacy_scanner(resolved_memory_policy().privacy),
            semantic_classifier=MagicMock(),
            approved_memory_repository=MagicMock(),
            memory_embedder=lambda _text: [0.0],
            memory_formation_submitter=scheduler,
            clock=_fixture_now,
            tools=tools,
            life_context=None,
            response_provenance_recorder=None,
            response_history_filter=None,
        ),
    )


class _RecordingHistorySession:
    """実 HistorySession へ委譲し、開始・完了保存の呼出を記録するspy。"""

    def __init__(self, inner: HistorySession | None = None) -> None:
        # Session入口では open_session の呼出時に実sessionが決まるため、
        # 構築時点は未設定でもよい。
        self._inner = inner
        self.calls: list[tuple[str, str]] = []

    def start_turn(self, user_content: str):
        started = self._inner.start_turn(user_content)
        self.calls.append(("start", str(started.turn_id)))
        return started

    def complete_turn(self, started_turn, assistant_content: str):
        turn = self._inner.complete_turn(started_turn, assistant_content)
        self.calls.append(("complete", str(turn.turn_id)))
        return turn

    def fail_turn(self, started_turn) -> None:
        self._inner.fail_turn(started_turn)

    def interrupt_turn(
        self,
        started_turn,
        generated_text,
        response_audio_segments,
        last_played_audio_sequence,
    ):
        return self._inner.interrupt_turn(
            started_turn,
            generated_text,
            response_audio_segments,
            last_played_audio_sequence,
        )

    def mark_screen_derived(self, started_turn, lineages) -> None:
        self._inner.mark_screen_derived(started_turn, lineages)

    def prompt_turns(self, *, max_completed_turns: int, page_size: int):
        return self._inner.prompt_turns(
            max_completed_turns=max_completed_turns, page_size=page_size
        )


class _RecordingHistoryService:
    """open_session の返り値をspyで包み、生成・永続化の両経路を観測する。"""

    def __init__(
        self,
        inner: ConversationHistoryService,
        recorder: _RecordingHistorySession,
    ) -> None:
        self._inner = inner
        self._recorder = recorder

    def open_session(self, character_id: str, conversation_id: UUID):
        self._recorder._inner = self._inner.open_session(character_id, conversation_id)
        return self._recorder


def _session_factory(
    *,
    conversation_history_database_path,
    repository,
    scheduler: RecordingFormationScheduler,
    invocations: list[CoreInvocation] | None = None,
    history_recorder: _RecordingHistorySession | None = None,
    tools=None,
    transcriber=None,
    memory_outcome=None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> ProductionConversationCoreSessionFactory:
    """本番 factory を、実 ChatService と同じ callback 配線で構築する。

    `main._create_core_session_factory` と同じ構造の
    generate_screen_reply_stream を渡し、Session入口が実物の
    `ChatService.prepare_core_reply` から Provider まで進むようにする。
    CoreInvocation は委譲するspyで記録する。
    """
    history_service = ConversationHistoryService(
        repository,
        create_history_sanitizer(
            create_privacy_scanner(resolved_memory_policy().privacy),
            resolved_memory_policy().privacy,
        ),
    )
    model_settings = _fixture_model_settings()
    if memory_outcome is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(
            "app._chat_runtime._rag_service.retrieve_prompt_memories",
            lambda *args, **kwargs: memory_outcome,
        )
    chroma_path = Path(str(conversation_history_database_path)).parent / "chroma"
    chat_service = _fixture_chat_service(
        history_service=history_service,
        scheduler=scheduler,
        tools=tools,
        memory_outcome=memory_outcome,
        chroma_path=chroma_path,
    )
    if invocations is not None:
        original_prepare_core_reply = chat_service.prepare_core_reply

        async def prepare_core_reply_spy(
            invocation: CoreInvocation,
            history_session: HistorySession,
            **kwargs,
        ):
            invocations.append(invocation)
            return await original_prepare_core_reply(
                invocation, history_session, **kwargs
            )

        if monkeypatch is not None:
            monkeypatch.setattr(
                chat_service, "prepare_core_reply", prepare_core_reply_spy
            )
        else:
            chat_service.prepare_core_reply = prepare_core_reply_spy

    async def generate_screen_reply_stream(
        session_id: str,
        client_session_id,
        character: str,
        conversation_id,
        history_session,
        transcript: str,
        screen_lineage_observer,
        prompt_observer,
        response,
    ):
        # main._create_core_session_factory が行う正式音声入口と同じ構造にする。
        history_access = None
        screen_material = None
        async for text in _stream_core_reply(
            chat_service,
            model_settings,
            character,
            history_session,
            transcript,
            screen_material,
            history_access,
            screen_lineage_observer,
            tools=tools,
            conversation_id=str(conversation_id),
            prompt_observer=prompt_observer,
            core_invocation=CoreInvocation.owner_voice(
                character_id=character,
                conversation_id=conversation_id,
                message=transcript,
                input_ids=tuple(source.input_id for source in response.source_inputs),
                response_id=response.response_id,
            ),
        ):
            yield text

    return ProductionConversationCoreSessionFactory(
        transcriber=transcriber or FixtureSyncTranscriber(),
        synthesizer=FixtureSpeakerSynthesizer(),
        history_service=(
            _RecordingHistoryService(history_service, history_recorder)
            if history_recorder is not None
            else history_service
        ),
        completed_turn_observer=make_formation_observer(scheduler, repository),
        response_provenance_recorder=None,
        generate_screen_reply_stream=generate_screen_reply_stream,
        measurement_kind="automated_test",
    )


def _load_fixture_character(character: str):
    """テスト用の Card loader。"""
    if character != CHARACTER_ID:
        raise FileNotFoundError(character)
    from app._chat_runtime import CharacterRuntimeDefinition

    card = MagicMock()
    card.data.character_book = None
    return CharacterRuntimeDefinition(
        prompt=fixture_character_prompt(),
        character_book=card.data.character_book,
    )


async def _unused_llm_generator(*_args, **_kwargs):
    raise AssertionError("LLM generation must go through fake Provider")


def _fixture_token_counter(messages) -> int:
    return len(messages)


def _fixture_now():
    return datetime(2026, 9, 28, 0, 0, tzinfo=UTC)


def _fixture_model_settings() -> ModelSettings:
    return ModelSettings(
        whisper_model="large-v3",
        chat_context_tokens=7168 + 1024,
        assistant_max_generation_tokens=1024,
        max_completed_turns=10,
        history_token_limit=4096,
        user_input_token_limit=8192,
        model_context_token_limit=32768,
    )


def _session_with_history(
    conversation_history_database_path,
    *,
    scheduler: RecordingFormationScheduler | None = None,
    invocations=None,
    history_recorder: _RecordingHistorySession | None = None,
    tools=None,
    transcriber=None,
    memory_outcome=None,
    monkeypatch: pytest.MonkeyPatch | None = None,
):
    """実履歴永続化を伴う、本番factory経由の ConversationCoreSession を構築する。

    戻り値は (factory, repository, scheduler)。factory._tts_adapter は
    VoicevoxTtsAdapter が必要なため、Card repo root を fixture Card へ
    切り替えてから factory.create を呼ぶ必要がある。
    """
    repository = create_repository(conversation_history_database_path)
    scheduler = scheduler or RecordingFormationScheduler()
    factory = _session_factory(
        conversation_history_database_path=conversation_history_database_path,
        repository=repository,
        scheduler=scheduler,
        invocations=invocations,
        history_recorder=history_recorder,
        tools=tools,
        transcriber=transcriber,
        memory_outcome=memory_outcome,
        monkeypatch=monkeypatch,
    )
    return factory, repository, scheduler


def _build_session(
    factory: ProductionConversationCoreSessionFactory, *, tmp_path, monkeypatch
):
    """fixture Card を読ませて session を組み立てる。"""
    write_character_card(tmp_path, CHARACTER_ID, fixture_character_document())
    use_character_repo_root(monkeypatch, tmp_path)
    delivery = FixtureDelivery()
    session = factory.create(
        session_id="conformance-session",
        character_id=CHARACTER_ID,
        conversation_id=CONVERSATION_ID,
        delivery=delivery,
    )
    return session, delivery


async def _wait_terminal(session, response_id: str, timeout: float = 0.5) -> None:
    """responseがterminal状態になるまで待つ。"""

    async def poll() -> None:
        while not session.response(response_id).state.is_terminal:
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


def _prompt_message_contents(adapter: CapturingOllamaAdapter) -> list[str]:
    """fake Providerが受け取った最終promptのcontents一覧。"""
    return [m.content for m in adapter.last_messages()]


def _assert_prompt_contains_fixture(adapter: CapturingOllamaAdapter) -> None:
    """C2: prompt に Card由来・履歴・memory・現在入力が含まれることを検証する。"""
    contents = _prompt_message_contents(adapter)
    assert contents, "Provider request was not recorded"
    # Card由来 system prompt
    assert any(CHARACTER_SYSTEM_PROMPT in c for c in contents), contents
    # memory
    assert any(MEMORY_HEADING in c for c in contents), contents
    assert any(MEMORY_CONTENT in c for c in contents), contents
    # history (raw値がpromptに入る。sanitizeは保存時のみ)
    assert any(RESTORED_USER in c for c in contents), contents
    assert any(RESTORED_ASSISTANT in c for c in contents), contents
    # current user input
    assert contents[-1] == USER_TEXT, contents


def _fixture_key_messages(adapter: CapturingOllamaAdapter) -> tuple[str, ...]:
    """入口間照合のために、fixture由来要素を含むmessageだけを抽出する。"""
    return tuple(
        content
        for content in _prompt_message_contents(adapter)
        if any(
            marker in content
            for marker in (
                CHARACTER_SYSTEM_PROMPT,
                MEMORY_CONTENT,
                RESTORED_USER,
                RESTORED_ASSISTANT,
                USER_TEXT,
            )
        )
    )


def _insert_fixture_history(database_path) -> None:
    insert_completed_turn(
        database_path,
        character_id=CHARACTER_ID,
        conversation_id=CONVERSATION_ID,
        user_content=RESTORED_USER,
        assistant_content=RESTORED_ASSISTANT,
    )


def _attach_http_tool_service(app, tools) -> None:
    """HTTP入口の `app.state.tool_service` と実 ChatService へ同じtoolsを配線する。"""
    app.state.tool_service = tools
    app.state.chat_service.tools = tools


def _detach_http_tool_service(app) -> None:
    app.state.chat_service.tools = None
    app.state.tool_service = None


def _wrap_open_session_with_spy(service, recorded: list[tuple[str, str]]) -> None:
    """ChatServiceが `open_session` で受け取る HistorySession をspyで包む。"""
    history_service = service._conversation_history_service
    inner_open = history_service.open_session

    def open_session(character_id, conversation_id):
        inner = inner_open(character_id, conversation_id)
        spy = _RecordingHistorySession(inner)

        def start_turn(content):
            started = spy._inner.start_turn(content)
            recorded.append(("start", str(started.turn_id)))
            return started

        def complete_turn(started, content):
            turn = spy._inner.complete_turn(started, content)
            recorded.append(("complete", str(turn.turn_id)))
            return turn

        spy.start_turn = start_turn
        spy.complete_turn = complete_turn
        return spy

    history_service.open_session = open_session


def _screen_session_start_body(
    client_session_id: str, routing_revision: str
) -> dict[str, object]:
    return {
        "protocol_version": "1.0",
        "type": "screen_session_start_requested",
        "event_id": str(uuid4()),
        "client_session_id": client_session_id,
        "generation": 1,
        "character_id": CHARACTER_ID,
        "conversation_id": str(CONVERSATION_ID),
        "requested_surface": "monitor",
        "actual_surface": "monitor",
        "routing_revision": routing_revision,
        "cloud_consent": {
            "cloud_vision": False,
            "cloud_derived_chat": False,
        },
    }


def _snapshot_upload_headers(
    *,
    request_id: str,
    turn_id: str,
    screen_session_id: str,
    client_session_id: str,
    image_id: str,
    captured_at: str,
    body_length: int,
) -> dict[str, str]:
    return {
        "Origin": "http://localhost:5173",
        "Content-Type": "image/png",
        "Content-Length": str(body_length),
        "x-screen-protocol-version": "1.0",
        "x-screen-event-id": str(uuid4()),
        "x-screen-session-id": screen_session_id,
        "x-screen-client-session-id": client_session_id,
        "x-screen-generation": "1",
        "x-screen-turn-id": turn_id,
        "x-screen-image-id": image_id,
        "x-screen-surface": "monitor",
        "x-screen-captured-at": captured_at,
        "x-screen-width": "4",
        "x-screen-height": "4",
    }


def _fixture_png() -> bytes:
    """`validate_multimodal_messages` を通る最小PNG。"""
    import io

    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 255, 255)).save(output, format="PNG")
    return output.getvalue()


def _fixture_vision_client():
    """VISION応答を決定的に返す fake VisionInferenceClient。

    `ScreenPerceptionService._vision` は lifespan 内で runtime router へ接続
    済みのため、`_fake_router_context` の register だけでは到達しない。
    画像検証・lineage生成は実 service を通し、Vision 呼出だけを差し替える。
    """
    from app.screen_perception.vision import (
        VisionObservation,
        VisionTargetCandidate,
    )

    class FixtureVision:
        def observe(self, **_request):
            return VisionObservation(
                target_status="identified",
                candidates=(
                    VisionTargetCandidate(
                        label="対象ウィンドウ",
                        location="center",
                        recognized_content="fixture画面",
                        evidence="fixture evidence",
                        limitations=(),
                    ),
                ),
                unreadable_reasons=(),
                uncertainty="fixture observation",
            )

    return FixtureVision()


def _initialize_entry_data_root(monkeypatch: pytest.MonkeyPatch, data_root):
    """入口別の独立data rootを初期化し、fixture履歴を挿入してDB pathを返す。"""
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))
    paths = resolve_runtime_paths(os.environ, REPOSITORY_ROOT)
    initialize_runtime_data_root(paths, REPOSITORY_ROOT)
    initialize_conversation_history_schema(paths.sqlite_path)
    with sqlite3.connect(paths.sqlite_path) as connection:
        connection.execute(
            "INSERT INTO conversations "
            "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
            (CHARACTER_ID, str(CONVERSATION_ID), "2026-08-01T00:00:00.000000Z"),
        )
    _insert_fixture_history(paths.sqlite_path)
    return paths


# ---------------------------------------------------------------------------
# 正常系: 同一fixtureを3入口に通す
# ---------------------------------------------------------------------------


class TestNormalEquivalence:
    """C1〜C6: 3入口が同一fixtureで同じ振る舞いをする。"""

    def test_http_entry_produces_completed_turn_with_fixture_prompt(
        self, conversation_history_database_path, runtime_paths, monkeypatch
    ):
        """HTTP POST /chat が fixture を通して completed turn を残す。"""
        adapter = CapturingOllamaAdapter()
        scheduler = _patch_formation_scheduler(monkeypatch)
        _patch_memory_and_card(monkeypatch, runtime_paths)
        _insert_fixture_history(conversation_history_database_path)
        http_saves: list[tuple[str, str]] = []
        from app.main import app

        with TestClient(app) as client:
            # HTTP入口の CoreInvocation を観測し、空の input_ids を確認する。
            http_invocations: list[CoreInvocation] = []
            service = app.state.chat_service
            original_prepare = service.prepare_core_reply

            async def record_invocation(invocation, history_session, **kwargs):
                http_invocations.append(invocation)
                return await original_prepare(invocation, history_session, **kwargs)

            monkeypatch.setattr(service, "prepare_core_reply", record_invocation)
            _wrap_open_session_with_spy(service, http_saves)
            with _fake_router_context(adapter):
                response = client.post(
                    "/chat",
                    json={
                        "character": CHARACTER_ID,
                        "conversation_id": str(CONVERSATION_ID),
                        "message": USER_TEXT,
                    },
                )
        assert response.status_code == 200
        body = response.json()
        assert body["character"] == CHARACTER_ID
        assert body["turn"]["kind"] == "content"
        assert body["turn"]["assistant_content"] == LLM_REPLY
        _assert_prompt_contains_fixture(adapter)
        # C6: HTTP /chat は入力IDを持たない入口のため input_ids は空。
        assert len(http_invocations) == 1
        assert http_invocations[0].input_ids == ()
        assert http_invocations[0].actor.platform == "web"
        turns = fetch_turns(
            conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
        )
        assert len(turns) == 2
        # 既存turnはSQL直接挿入のためsanitize済みではなくraw値が残る
        assert turns[0]["status"] == "completed"
        assert turns[0]["user_content"] == RESTORED_USER
        assert turns[0]["assistant_content"] == RESTORED_ASSISTANT
        assert turns[1]["status"] == "completed"
        assert turns[1]["user_content"] == USER_TEXT
        assert turns[1]["assistant_content"] == LLM_REPLY
        # 正常完了では同一turnへ開始保存1回・完了保存1回が行われる。
        assert http_saves == [
            ("start", turns[1]["turn_id"]),
            ("complete", turns[1]["turn_id"]),
        ]
        assert len(scheduler.jobs) == 1

    def test_http_processing_to_completed_transition(
        self, conversation_history_database_path, runtime_paths, monkeypatch
    ):
        """同一turn IDが Provider呼出時点で processing、終端後に completed。"""
        adapter = CapturingOllamaAdapter()
        scheduler = _patch_formation_scheduler(monkeypatch)
        _patch_memory_and_card(monkeypatch, runtime_paths)
        _insert_fixture_history(conversation_history_database_path)
        statuses_during_provider: list[str | None] = []

        def probe() -> None:
            turns = fetch_turns(
                conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
            )
            statuses_during_provider.append(
                turns[-1]["status"] if len(turns) > 1 else None
            )

        adapter.status_probe = probe
        from app.main import app

        with TestClient(app) as client:
            with _fake_router_context(adapter):
                response = client.post(
                    "/chat",
                    json={
                        "character": CHARACTER_ID,
                        "conversation_id": str(CONVERSATION_ID),
                        "message": USER_TEXT,
                    },
                )
        assert response.status_code == 200
        assert statuses_during_provider == ["processing"]
        turns = fetch_turns(
            conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
        )
        assert len(turns) == 2
        assert turns[1]["status"] == "completed"
        assert (
            turn_status(
                conversation_history_database_path,
                CHARACTER_ID,
                CONVERSATION_ID,
                turns[1]["turn_id"],
            )
            == "completed"
        )
        assert len(scheduler.jobs) == 1

    def test_session_text_entry_produces_completed_turn(
        self, conversation_history_database_path, tmp_path, monkeypatch
    ):
        """Session Text (submit_text) が本番factory経由で completed turn を残す。"""
        scheduler = RecordingFormationScheduler()
        invocations: list[CoreInvocation] = []
        recorder = _RecordingHistorySession()
        adapter = CapturingOllamaAdapter()
        factory, repository, _ = _session_with_history(
            conversation_history_database_path,
            scheduler=scheduler,
            invocations=invocations,
            history_recorder=recorder,
            memory_outcome=fixture_retrieval_outcome(),
            monkeypatch=monkeypatch,
        )
        session, delivery = _build_session(
            factory, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        _insert_fixture_history(conversation_history_database_path)

        async def exercise():
            response = await session.submit_text(input_id=TEXT_INPUT_ID, text=USER_TEXT)
            await _wait_terminal(session, response.response_id)
            await session.end()
            return response

        with _fake_router_context(adapter):
            response = _run(exercise())

        assert response is not None
        assert session.response(response.response_id).state is ResponseState.COMPLETED
        assert [s.input_id for s in response.source_inputs] == [TEXT_INPUT_ID]
        _assert_prompt_contains_fixture(adapter)
        turns = fetch_turns(
            conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
        )
        assert len(turns) == 2
        assert turns[1]["status"] == "completed"
        assert turns[1]["user_content"] == USER_TEXT
        assert turns[1]["assistant_content"] == LLM_REPLY
        # 正常完了では同一turnへ開始保存1回・完了保存1回が行われる。
        assert recorder.calls == [
            ("start", turns[1]["turn_id"]),
            ("complete", turns[1]["turn_id"]),
        ]
        assert len(scheduler.jobs) == 1
        assert len(invocations) == 1
        assert invocations[0].input_ids == (TEXT_INPUT_ID,)
        assert invocations[0].response_id == response.response_id

    def test_speech_entry_produces_completed_turn(
        self, conversation_history_database_path, tmp_path, monkeypatch
    ):
        """Speech (start_transcription → fake STT確定文字列) が completed turn を残す。

        Provider呼出時点で同一turn IDが `processing`、終端後に `completed`
        であることをprobeで照合する。
        """
        scheduler = RecordingFormationScheduler()
        invocations: list[CoreInvocation] = []
        recorder = _RecordingHistorySession()
        adapter = CapturingOllamaAdapter()
        factory, repository, _ = _session_with_history(
            conversation_history_database_path,
            scheduler=scheduler,
            invocations=invocations,
            history_recorder=recorder,
            memory_outcome=fixture_retrieval_outcome(),
            monkeypatch=monkeypatch,
        )
        session, delivery = _build_session(
            factory, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        _insert_fixture_history(conversation_history_database_path)
        provider_turn_states: list[tuple[str | None, str | None]] = []

        def probe() -> None:
            turns = fetch_turns(
                conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
            )
            provider_turn_states.append(
                (
                    turns[-1]["turn_id"] if len(turns) > 1 else None,
                    turns[-1]["status"] if len(turns) > 1 else None,
                )
            )

        adapter.status_probe = probe

        async def exercise():
            task = session.start_transcription(
                utterance_id="speech-utterance-538-001",
                audio=b"speech-audio",
                should_response=True,
            )
            response = await task
            assert response is not None
            await _wait_terminal(session, response.response_id)
            await session.end()
            return response

        with _fake_router_context(adapter):
            response = _run(exercise())

        assert response is not None
        assert session.response(response.response_id).state is ResponseState.COMPLETED
        assert [s.input_id for s in response.source_inputs] == [
            "speech-utterance-538-001"
        ]
        _assert_prompt_contains_fixture(adapter)
        turns = fetch_turns(
            conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
        )
        assert len(turns) == 2
        # Provider呼出時点の同一turn IDが processing、終端後は completed。
        assert provider_turn_states == [(turns[1]["turn_id"], "processing")]
        # Speechでも同一turnへ開始保存1回・完了保存1回が行われる。
        assert recorder.calls == [
            ("start", turns[1]["turn_id"]),
            ("complete", turns[1]["turn_id"]),
        ]
        assert turns[1]["status"] == "completed"
        assert turns[1]["user_content"] == USER_TEXT
        assert turns[1]["assistant_content"] == LLM_REPLY
        assert (
            turn_status(
                conversation_history_database_path,
                CHARACTER_ID,
                CONVERSATION_ID,
                turns[1]["turn_id"],
            )
            == "completed"
        )
        assert len(scheduler.jobs) == 1
        assert len(invocations) == 1
        assert invocations[0].input_ids == ("speech-utterance-538-001",)
        assert invocations[0].response_id == response.response_id


# ---------------------------------------------------------------------------
# processing → completed の Session遷移とイベント
# ---------------------------------------------------------------------------


class TestSessionTransitionAndIdentifiers:
    """C3/C6: Sessionで同一turnの遷移と入口固有IDを観測する。"""

    def test_session_text_processing_then_completed_with_identifiers(
        self, conversation_history_database_path, tmp_path, monkeypatch
    ):
        """同一turnが Provider呼出中に processing、終端後に completed。"""
        scheduler = RecordingFormationScheduler()
        invocations: list[CoreInvocation] = []
        adapter = CapturingOllamaAdapter()
        statuses_during_provider: list[str | None] = []

        def probe() -> None:
            turns = fetch_turns(
                conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
            )
            statuses_during_provider.append(
                turns[-1]["status"] if len(turns) > 1 else None
            )

        adapter.status_probe = probe
        factory, repository, _ = _session_with_history(
            conversation_history_database_path,
            scheduler=scheduler,
            invocations=invocations,
        )
        session, delivery = _build_session(
            factory, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        _insert_fixture_history(conversation_history_database_path)

        async def exercise():
            response = await session.submit_text(input_id=TEXT_INPUT_ID, text=USER_TEXT)
            await _wait_terminal(session, response.response_id)
            await session.end()
            return response

        with _fake_router_context(adapter):
            response = _run(exercise())

        assert response is not None
        assert statuses_during_provider == ["processing"]
        turns = fetch_turns(
            conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
        )
        assert len(turns) == 2
        assert turns[1]["status"] == "completed"
        assert (
            turn_status(
                conversation_history_database_path,
                CHARACTER_ID,
                CONVERSATION_ID,
                turns[1]["turn_id"],
            )
            == "completed"
        )
        events = [e for e in delivery.events if isinstance(e, CoreEvent)]
        started = [e for e in events if e.type == "response_started"]
        assert len(started) == 1
        assert started[0].response_id == response.response_id
        assert started[0].source_inputs[0].input_id == TEXT_INPUT_ID
        assert invocations[0].input_ids == (TEXT_INPUT_ID,)
        assert invocations[0].response_id == response.response_id


# ---------------------------------------------------------------------------
# screen由来turnは形成予約しない
# ---------------------------------------------------------------------------


class TestScreenDerivedFormationSkip:
    """C5: screen由来turnは形成予約されない。"""

    def test_http_screen_derived_turn_does_not_submit_formation(
        self, conversation_history_database_path, runtime_paths, monkeypatch
    ):
        """HTTPで画像受付まで進め、保存済み画面由来turnが形成予約0になる。

        `/chat` の 202 受付ではturnはまだ保存されない。実経路
        `/perception/screen/requests/{id}/image` → `accept_image` →
        `_complete_text_chat` → `generate_screen_chat_reply` →
        `submit_completed_turn` まで通し、完了turnの画面由来判定と予約回数を
        同じ操作で観測する。
        """
        adapter = CapturingOllamaAdapter()
        scheduler = _patch_formation_scheduler(monkeypatch)
        _patch_memory_and_card(monkeypatch, runtime_paths)
        monkeypatch.setenv("INFERENCE_TARGET_VISION", "ollama/fixture-vision")
        monkeypatch.setenv("INFERENCE_TARGET_VISION_MAX_INPUT_TOKENS", "4096")
        monkeypatch.setenv("INFERENCE_TARGET_VISION_MAX_OUTPUT_TOKENS", "512")
        _insert_fixture_history(conversation_history_database_path)
        from app.main import app

        with TestClient(app, base_url="http://localhost:5173") as client:
            repository = app.state.conversation_history_repository
            # lifespanでruntime routerへ接続済みのVision clientをfakeへ差し替える。
            monkeypatch.setattr(
                app.state.screen_perception_service,
                "_vision",
                _fixture_vision_client(),
            )
            routing = client.get("/perception/screen/routing")
            assert routing.status_code == 200
            client_session_id = routing.json()["client_session_id"]
            routing_revision = routing.json()["routing_revision"]
            started = client.post(
                "/perception/screen/sessions",
                headers={"Origin": "http://localhost:5173"},
                json=_screen_session_start_body(client_session_id, routing_revision),
            )
            assert started.status_code == 201
            screen_session_id = started.json()["screen_session_id"]
            with _fake_router_context(adapter):
                accepted = client.post(
                    "/chat",
                    json={
                        "character": CHARACTER_ID,
                        "conversation_id": str(CONVERSATION_ID),
                        "message": USER_TEXT,
                        "screen_reference": True,
                        "screen_client_session_id": client_session_id,
                    },
                    headers={"Origin": "http://localhost:5173"},
                )
                assert accepted.status_code == 202
                request_id = accepted.json()["request_id"]
                turn_id = accepted.json()["turn_id"]
                image = _fixture_png()
                uploaded = client.put(
                    f"/perception/screen/requests/{request_id}/image",
                    headers=_snapshot_upload_headers(
                        request_id=request_id,
                        turn_id=turn_id,
                        screen_session_id=screen_session_id,
                        client_session_id=client_session_id,
                        image_id=str(uuid4()),
                        captured_at=datetime.now(UTC)
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z"),
                        body_length=len(image),
                    ),
                    content=image,
                )
        assert uploaded.status_code == 200
        body = uploaded.json()
        assert body["character"] == CHARACTER_ID
        assert body["turn"]["kind"] == "content"
        assert body["turn"]["assistant_content"] == LLM_REPLY
        turns = fetch_turns(
            conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
        )
        assert len(turns) == 2
        assert turns[1]["status"] == "completed"
        assert turns[1]["user_content"] == USER_TEXT
        assert repository.is_screen_derived(
            CHARACTER_ID, CONVERSATION_ID, UUID(turns[1]["turn_id"])
        )
        assert len(scheduler.jobs) == 0

    def test_session_screen_derived_turn_does_not_submit_formation(
        self, conversation_history_database_path, tmp_path, monkeypatch
    ):
        """Sessionで保存済み画面由来turnが形成予約0になる。

        先に画面由来の完了turnを永続化しておくと、続く応答のpromptが
        follow-up lineage を保持し、screen_lineage_observer がそのlineageを
        記録する。persistence が mark_screen_derived → is_screen_derived
        →0件へ進む、実製品と同じ経路で確認する。
        """
        prior_turn_lineage = ScreenLineage(
            screen_lineage_id=UUID("31000000-0000-4000-8000-000000000001"),
            origin_screen_session_id=UUID("32000000-0000-4000-8000-000000000001"),
            origin_generation=1,
            origin_routing_revision="a" * 64,
            source="explicit_ui",
            surface="monitor",
        )
        scheduler = RecordingFormationScheduler()
        factory, repository, _ = _session_with_history(
            conversation_history_database_path,
            scheduler=scheduler,
        )
        session, delivery = _build_session(
            factory, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        _insert_fixture_history(conversation_history_database_path)
        prior_turn = fetch_turns(
            conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
        )[-1]
        repository.mark_screen_derived(
            CHARACTER_ID,
            CONVERSATION_ID,
            UUID(prior_turn["turn_id"]),
            (prior_turn_lineage,),
        )

        async def exercise():
            response = await session.submit_text(input_id=TEXT_INPUT_ID, text=USER_TEXT)
            await _wait_terminal(session, response.response_id)
            await session.end()
            return response

        adapter = CapturingOllamaAdapter()
        with _fake_router_context(adapter):
            response = _run(exercise())

        assert response is not None
        assert session.response(response.response_id).state is ResponseState.COMPLETED
        turns = fetch_turns(
            conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
        )
        assert len(turns) == 2
        assert turns[1]["status"] == "completed"
        assert repository.is_screen_derived(
            CHARACTER_ID, CONVERSATION_ID, UUID(turns[1]["turn_id"])
        )
        assert len(scheduler.jobs) == 0


# ---------------------------------------------------------------------------
# 失敗・キャンセルでは形成予約しない
# ---------------------------------------------------------------------------


class TestFailureAndCancelFormationSkip:
    """C5: 失敗・キャンセルでは形成予約が0。"""

    def test_http_llm_failure_does_not_submit_formation(
        self, conversation_history_database_path, runtime_paths, monkeypatch
    ):
        """HTTPでLLM失敗時に形成予約が0になり、turnはfailedになる。"""
        scheduler = _patch_formation_scheduler(monkeypatch)
        _patch_memory_and_card(monkeypatch, runtime_paths)
        _insert_fixture_history(conversation_history_database_path)
        from app.inference.errors import InferenceError, InferenceErrorCategory

        failing_adapter = CapturingOllamaAdapter()
        failing_adapter.generate_text = lambda request: (_ for _ in ()).throw(
            InferenceError(
                InferenceErrorCategory.PROVIDER_ERROR,
                retryable=False,
            )
        )
        from app.main import app

        with TestClient(app) as client:
            with _fake_router_context(failing_adapter):
                response = client.post(
                    "/chat",
                    json={
                        "character": CHARACTER_ID,
                        "conversation_id": str(CONVERSATION_ID),
                        "message": USER_TEXT,
                    },
                )
        assert response.status_code == 502
        turns = fetch_turns(
            conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
        )
        assert len(turns) == 2
        assert turns[1]["status"] == "failed"
        assert len(scheduler.jobs) == 0

    def test_session_cancel_does_not_submit_formation(
        self, conversation_history_database_path, tmp_path, monkeypatch
    ):
        """Sessionでcancel時に形成予約が0になり、turnはinterruptedになる。"""
        scheduler = RecordingFormationScheduler()
        adapter = CapturingOllamaAdapter()
        factory, repository, _ = _session_with_history(
            conversation_history_database_path,
            scheduler=scheduler,
        )
        session, delivery = _build_session(
            factory, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        _insert_fixture_history(conversation_history_database_path)

        async def exercise():
            response = await session.submit_text(input_id=TEXT_INPUT_ID, text=USER_TEXT)
            await session.cancel_response(
                response_id=response.response_id, reason="barge_in"
            )
            await _wait_terminal(session, response.response_id)
            await session.end()
            return response

        with _fake_router_context(adapter):
            response = _run(exercise())

        assert response is not None
        assert session.response(response.response_id).state is ResponseState.CANCELLED
        turns = fetch_turns(
            conversation_history_database_path, CHARACTER_ID, CONVERSATION_ID
        )
        assert len(turns) == 2
        assert turns[1]["status"] == "interrupted"
        assert len(scheduler.jobs) == 0


# ---------------------------------------------------------------------------
# Tool dispatch 回数の観測
# ---------------------------------------------------------------------------


class TestToolDispatchCount:
    """C5: 同一Tool判断を3入口へ通し、実dispatch先の呼出回数を照合する。"""

    def test_http_dispatches_each_decided_tool_call(
        self,
        conversation_history_database_path,
        runtime_paths,
        monkeypatch,
    ):
        """HTTP /chat で決定されたtool callがfake sourceへ届く。

        `tools.run` はHTTP handler task上でawaitされ、そのまま実ToolServiceを
        経由して ExecutionGate → fake source へ到達する。
        """
        adapter = CapturingOllamaAdapter()
        _patch_formation_scheduler(monkeypatch)
        _patch_memory_and_card(monkeypatch, runtime_paths)
        _insert_fixture_history(conversation_history_database_path)
        decisions = Decisions(call, ToolDecision("finish"))
        calls_seen: list[tuple] = []

        async def scenario():
            async with tool_runtime(decisions) as (tools, source, _gate):
                from app.main import app

                with TestClient(app) as client:
                    _attach_http_tool_service(app, tools)
                    try:
                        with _fake_router_context(adapter):
                            response = client.post(
                                "/chat",
                                json={
                                    "character": CHARACTER_ID,
                                    "conversation_id": str(CONVERSATION_ID),
                                    "message": USER_TEXT,
                                },
                            )
                    finally:
                        _detach_http_tool_service(app)
                calls_seen.extend(c for c in source.calls if c[0] == "native-tool")
                return response

        response = _run(asyncio.wait_for(scenario(), timeout=60))
        assert response.status_code == 200
        assert response.json()["turn"]["assistant_content"] == LLM_REPLY
        assert len(calls_seen) == 1

    def test_session_text_dispatches_each_decided_tool_call(
        self, conversation_history_database_path, tmp_path, monkeypatch
    ):
        """Session Text で決定されたtool callだけがfake sourceへ届く。"""
        scheduler = RecordingFormationScheduler()
        invocations: list[CoreInvocation] = []
        adapter = CapturingOllamaAdapter()
        decisions = Decisions(call, ToolDecision("finish"))

        async def scenario():
            async with tool_runtime(decisions) as (tools, source, _gate):
                factory, repository, _ = _session_with_history(
                    conversation_history_database_path,
                    scheduler=scheduler,
                    invocations=invocations,
                    tools=tools,
                    memory_outcome=fixture_retrieval_outcome(),
                    monkeypatch=monkeypatch,
                )
                session, _delivery = _build_session(
                    factory, tmp_path=tmp_path, monkeypatch=monkeypatch
                )
                _insert_fixture_history(conversation_history_database_path)
                response = await session.submit_text(
                    input_id=TEXT_INPUT_ID, text=USER_TEXT
                )
                await _wait_terminal(session, response.response_id)
                await session.end()
                return response, source, session.response(response.response_id).state

        with _fake_router_context(adapter):
            response, source, final_state = _run(scenario())

        assert response is not None
        assert final_state is ResponseState.COMPLETED
        # 決定されたtool callはちょうど1回だけ実dispatch先へ届く。
        tool_dispatches = [c for c in source.calls if c[0] == "native-tool"]
        assert len(tool_dispatches) == 1

    def test_speech_dispatches_each_decided_tool_call(
        self, conversation_history_database_path, tmp_path, monkeypatch
    ):
        """Speechでも同じTool判断がfake sourceへ1回届く。"""
        scheduler = RecordingFormationScheduler()
        invocations: list[CoreInvocation] = []
        adapter = CapturingOllamaAdapter()
        decisions = Decisions(call, ToolDecision("finish"))

        async def scenario():
            async with tool_runtime(decisions) as (tools, source, _gate):
                factory, repository, _ = _session_with_history(
                    conversation_history_database_path,
                    scheduler=scheduler,
                    invocations=invocations,
                    tools=tools,
                    memory_outcome=fixture_retrieval_outcome(),
                    monkeypatch=monkeypatch,
                )
                session, _delivery = _build_session(
                    factory, tmp_path=tmp_path, monkeypatch=monkeypatch
                )
                _insert_fixture_history(conversation_history_database_path)
                task = session.start_transcription(
                    utterance_id="speech-utterance-538-001",
                    audio=b"speech-audio",
                    should_response=True,
                )
                response = await task
                assert response is not None
                await _wait_terminal(session, response.response_id)
                await session.end()
                return response, source, session.response(response.response_id).state

        with _fake_router_context(adapter):
            response, source, final_state = _run(scenario())

        assert response is not None
        assert final_state is ResponseState.COMPLETED
        tool_dispatches = [c for c in source.calls if c[0] == "native-tool"]
        assert len(tool_dispatches) == 1


# ---------------------------------------------------------------------------
# 3入口のprompt一致(C2の厳密版)
# ---------------------------------------------------------------------------


class TestPromptEquivalenceAcrossEntries:
    """C2: 3入口それぞれを独立した同一初期fixtureで実行し、promptを相互照合する。"""

    def test_entries_share_same_fixture_values(
        self, runtime_paths, monkeypatch, tmp_path
    ):
        """HTTP・Session Text・Speechでfixture由来要素が一致する。

        各入口は別のdata rootで起動し、同じCard・履歴・記憶・本文を与える。
        前入口が保存した生成turnは後続入口の初期履歴に混入しない。
        """
        observed: dict[str, tuple[str, ...]] = {}
        for entry in ("http", "text", "speech"):
            paths = _initialize_entry_data_root(
                monkeypatch, tmp_path / entry / "runtime-data"
            )
            adapter = CapturingOllamaAdapter()
            if entry == "http":
                _patch_formation_scheduler(monkeypatch)
                _patch_memory_and_card(monkeypatch, paths)
                from app.main import app

                with TestClient(app) as client:
                    with _fake_router_context(adapter):
                        response = client.post(
                            "/chat",
                            json={
                                "character": CHARACTER_ID,
                                "conversation_id": str(CONVERSATION_ID),
                                "message": USER_TEXT,
                            },
                        )
                assert response.status_code == 200
            else:
                factory, repository, _ = _session_with_history(
                    paths.sqlite_path,
                    scheduler=RecordingFormationScheduler(),
                    memory_outcome=fixture_retrieval_outcome(),
                    monkeypatch=monkeypatch,
                )
                card_dir = tmp_path / entry / "cards"
                write_character_card(
                    card_dir, CHARACTER_ID, fixture_character_document()
                )
                use_character_repo_root(monkeypatch, card_dir)
                delivery = FixtureDelivery()
                session = factory.create(
                    session_id="conformance-session",
                    character_id=CHARACTER_ID,
                    conversation_id=CONVERSATION_ID,
                    delivery=delivery,
                )

                async def exercise():
                    if entry == "text":
                        response = await session.submit_text(
                            input_id=TEXT_INPUT_ID, text=USER_TEXT
                        )
                    else:
                        task = session.start_transcription(
                            utterance_id="speech-utterance-538-001",
                            audio=b"speech-audio",
                            should_response=True,
                        )
                        response = await task
                    assert response is not None
                    await _wait_terminal(session, response.response_id)
                    await session.end()
                    return response

                with _fake_router_context(adapter):
                    _run(exercise())
            observed[entry] = _fixture_key_messages(adapter)
            # 初期履歴はfixture 1件のみで、生成turnは別turnとして追加される。
            turns = fetch_turns(paths.sqlite_path, CHARACTER_ID, CONVERSATION_ID)
            assert [t["user_content"] for t in turns][0] == RESTORED_USER
        # 各入口のpromptがfixtureの4要素をすべて含むことを先に確認し、
        # どの入口でも欠けた場合にvacuousな一致にならないようにする。
        for entry, keys in observed.items():
            assert keys, entry
            joined = "\n".join(keys)
            assert CHARACTER_SYSTEM_PROMPT in joined, (entry, keys)
            assert MEMORY_CONTENT in joined, (entry, keys)
            assert RESTORED_USER in joined, (entry, keys)
            assert RESTORED_ASSISTANT in joined, (entry, keys)
            assert USER_TEXT in keys[-1], (entry, keys)
        # 入口間でfixture由来要素が一致する。
        assert observed["http"] == observed["text"] == observed["speech"]
