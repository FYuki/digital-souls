import asyncio
from datetime import UTC, datetime
from unittest.mock import ANY, MagicMock, patch
from uuid import UUID
from pathlib import Path

import httpx
import pytest

from app import chat_service
from app import _chat_runtime
from app._chat_runtime import (
    ChatRuntimeDependencies,
    ChatRuntimeConfig,
    ChatService,
)
from app.chat_prompt import build_chat_prompt
from app.characters import loader as character_loader
from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatServiceError,
    ChatTimeoutError,
)
from app.conversation_history.prompt_history import RestoredHistoryTurn
from app.conversation_history.models import ConversationTurn, TurnStatus
from app.conversation_history.service import StartedHistoryTurn
from app.llm import router as llm_router
from app.memory.chroma_store import MemorySearchResult
from app.prompting import CharacterPrompt, PromptInputLimitError
from app.model_settings import resolve_model_settings
from app.privacy.contracts import HistoryDecisionReasonCode
from tests.conversation_history_test_support import CONVERSATION_ID
from tests.chat_reply_test_support import persisted_reply


_CHROMA_PATH = Path("/test/runtime-data/chroma")


_LOAD_PERSONALITY = "app.characters.loader.load_character_card"
_GENERATE_RESPONSE = "app.llm.router.generate_response"
_BUILD_AUGMENTED_SYSTEM_PROMPT = (
    "app._chat_runtime._rag_service.retrieve_prompt_memories"
)
_BUILD_PROMPT = "app.chat_prompt.PromptBuilder.build"
_PROMPT_CONFIG = resolve_model_settings({})


def _completed_turn(
    started_turn: StartedHistoryTurn,
    assistant_content: str,
) -> ConversationTurn:
    timestamp = datetime(2026, 8, 1, tzinfo=UTC)
    return ConversationTurn(
        turn_id=started_turn.turn_id,
        character_id="miori",
        conversation_id=CONVERSATION_ID,
        user_content="saved user content",
        assistant_content=assistant_content,
        status=TurnStatus.COMPLETED,
        privacy_reason_code=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


@pytest.fixture(autouse=True)
def _formal_token_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        llm_router,
        "count_input_tokens",
        lambda messages, *, settings: len(messages),
    )


def _character_card(system_prompt: str = "# prompt") -> MagicMock:
    card = MagicMock()
    card.to_character_prompt.return_value = CharacterPrompt(
        description="",
        personality="",
        scenario="",
        system_prompt=system_prompt,
        mes_example="",
        post_history_instructions="",
    )
    return card


def _runtime_dependencies(
    memory_formation_submitter: object | None = None,
) -> ChatRuntimeDependencies:
    from app.model_settings import resolve_model_settings

    settings = resolve_model_settings({})

    def load_prompt(character: str) -> CharacterPrompt:
        return character_loader.load_character_card(character).to_character_prompt()

    return ChatRuntimeDependencies(
        character_prompt_loader=load_prompt,
        prompt_builder=build_chat_prompt,
        llm_response_generator=lambda prompt, *, max_output_tokens: (
            llm_router.generate_response(
                prompt,
                max_output_tokens=max_output_tokens,
                settings=settings,
            )
        ),
        input_token_counter=lambda messages: llm_router.count_input_tokens(
            messages, settings=settings
        ),
        privacy_scanner=MagicMock(),
        semantic_classifier=MagicMock(),
        approved_memory_repository=MagicMock(),
        memory_formation_submitter=(
            memory_formation_submitter
            if memory_formation_submitter is not None
            else MagicMock()
        ),
    )


def _rag_memory(
    content: str,
    memory_id: str = "memory-1",
    *,
    occurred_at: str | None = "2026-07-31T00:00:00.000000Z",
) -> MemorySearchResult:
    from app.memory.persistence.contracts import TemporalPrecision
    from app.memory.rag_service import RetrievalMatchKind

    return MemorySearchResult(
        memory_id=memory_id,
        normalized_text=content,
        occurred_at=occurred_at,
        occurred_precision=(
            TemporalPrecision.DAY if occurred_at is not None else None
        ),
        match_kind=RetrievalMatchKind.SEMANTIC,
        memory_type="USER_PREFERENCE",
        raw_distance=1.25,
    )


def _rag_outcome(*memories: MemorySearchResult, no_match: bool = False):
    from app.memory.rag_service import RetrievalOutcome

    return RetrievalOutcome(memories, no_match)


def _generated_contents(generate: MagicMock) -> list[str]:
    prompt = generate.call_args.args[0]
    return [message.content for message in prompt.messages]


def _assistant_content(reply: chat_service.ChatReply) -> str:
    assert isinstance(reply.persisted_turn, chat_service.PersistedContentTurn)
    return reply.persisted_turn.assistant_content


class _IgnoringHistorySession:
    def start_turn(self, user_content: str) -> StartedHistoryTurn:
        return StartedHistoryTurn(
            UUID("00000000-0000-4000-8000-000000000001"),
            content_skipped=False,
        )

    def complete_turn(
        self,
        started_turn: StartedHistoryTurn,
        assistant_content: str,
    ) -> ConversationTurn:
        return _completed_turn(started_turn, assistant_content)

    def fail_turn(self, started_turn: StartedHistoryTurn) -> None:
        return None

    def prompt_turns(self, *, max_completed_turns: int, page_size: int):
        return iter(())


class _IgnoringHistoryService:
    def open_session(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> _IgnoringHistorySession:
        return _IgnoringHistorySession()


class _RecordingHistorySession:
    def __init__(self) -> None:
        self.started_turn = StartedHistoryTurn(
            UUID("00000000-0000-4000-8000-000000000002"),
            content_skipped=False,
        )
        self.start_calls: list[str] = []
        self.complete_calls: list[tuple[StartedHistoryTurn, str]] = []
        self.fail_calls: list[StartedHistoryTurn] = []
        self.restored_turns: tuple[RestoredHistoryTurn, ...] = ()

    def start_turn(self, user_content: str) -> StartedHistoryTurn:
        self.start_calls.append(user_content)
        return self.started_turn

    def complete_turn(
        self,
        started_turn: StartedHistoryTurn,
        assistant_content: str,
    ) -> ConversationTurn:
        self.complete_calls.append((started_turn, assistant_content))
        return _completed_turn(started_turn, assistant_content)

    def fail_turn(self, started_turn: StartedHistoryTurn) -> None:
        self.fail_calls.append(started_turn)

    def prompt_turns(self, *, max_completed_turns: int, page_size: int):
        return iter(self.restored_turns)


class _FailingCompleteHistorySession(_RecordingHistorySession):
    def __init__(self, error: RuntimeError) -> None:
        super().__init__()
        self.error = error

    def complete_turn(
        self,
        started_turn: StartedHistoryTurn,
        assistant_content: str,
    ) -> ConversationTurn:
        super().complete_turn(started_turn, assistant_content)
        raise self.error


class _PrivacySkippedHistorySession(_RecordingHistorySession):
    def complete_turn(
        self,
        started_turn: StartedHistoryTurn,
        assistant_content: str,
    ) -> ConversationTurn:
        self.complete_calls.append((started_turn, assistant_content))
        timestamp = datetime(2026, 8, 1, tzinfo=UTC)
        return ConversationTurn(
            turn_id=started_turn.turn_id,
            character_id="miori",
            conversation_id=CONVERSATION_ID,
            user_content=None,
            assistant_content=None,
            status=TurnStatus.PRIVACY_SKIPPED,
            privacy_reason_code=HistoryDecisionReasonCode.SCAN_FAILURE,
            sanitizer_version="test-sanitizer-v1",
            policy_version="test-policy-v1",
            created_at=timestamp,
            updated_at=timestamp,
        )


class _FailingCleanupHistorySession(_RecordingHistorySession):
    def __init__(self, error: RuntimeError) -> None:
        super().__init__()
        self.error = error

    def fail_turn(self, started_turn: StartedHistoryTurn) -> None:
        super().fail_turn(started_turn)
        raise self.error


class _FailingCompleteAndCleanupHistorySession(_FailingCompleteHistorySession):
    def __init__(
        self,
        complete_error: RuntimeError,
        cleanup_error: RuntimeError,
    ) -> None:
        super().__init__(complete_error)
        self.cleanup_error = cleanup_error

    def fail_turn(self, started_turn: StartedHistoryTurn) -> None:
        super().fail_turn(started_turn)
        raise self.cleanup_error


class _RecordingHistoryService:
    def __init__(self, session: _RecordingHistorySession) -> None:
        self.session = session

    def open_session(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> _RecordingHistorySession:
        return self.session


def _chat_service(rag_enabled: bool, policy=None) -> ChatService:
    return ChatService(
        ChatRuntimeConfig(
            rag_enabled=rag_enabled,
            memory_policy=policy,
            prompt_config=_PROMPT_CONFIG,
            chroma_path=_CHROMA_PATH,
        ),
        _IgnoringHistoryService(),
        _runtime_dependencies(),
    )


def _chat_service_with_history(session: _RecordingHistorySession) -> ChatService:
    return ChatService(
        ChatRuntimeConfig(
            rag_enabled=False,
            memory_policy=None,
            prompt_config=_PROMPT_CONFIG,
            chroma_path=_CHROMA_PATH,
        ),
        _RecordingHistoryService(session),
        _runtime_dependencies(),
    )


class TestChatServiceErrorContract:
    def test_uses_all_injected_runtime_dependencies_for_reply_generation(self):
        character_prompt = CharacterPrompt("", "", "", "injected", "", "")
        load_prompt = MagicMock(return_value=character_prompt)
        prompt_builder = MagicMock(wraps=build_chat_prompt)
        generate = MagicMock(return_value="injected reply")
        count = MagicMock(side_effect=lambda messages: len(messages))
        service = ChatService(
            ChatRuntimeConfig(
                rag_enabled=False,
                memory_policy=None,
                prompt_config=_PROMPT_CONFIG,
                chroma_path=_CHROMA_PATH,
            ),
            _IgnoringHistoryService(),
            ChatRuntimeDependencies(
                character_prompt_loader=load_prompt,
                prompt_builder=prompt_builder,
                llm_response_generator=generate,
                input_token_counter=count,
                privacy_scanner=MagicMock(),
                semantic_classifier=MagicMock(),
                approved_memory_repository=MagicMock(),
                memory_formation_submitter=MagicMock(),
            ),
        )

        reply = service.generate_chat_reply(
            "miori",
            CONVERSATION_ID,
            "hello",
        )

        assert _assistant_content(reply) == "injected reply"
        load_prompt.assert_called_once_with("miori")
        prompt_builder.assert_called_once()
        assert prompt_builder.call_args.kwargs["character"] is character_prompt
        assert count.call_count > 0
        generate.assert_called_once()
        assert generate.call_args.kwargs == {
            "max_output_tokens": _PROMPT_CONFIG.assistant_max_generation_tokens
        }

    def test_input_limit_error_exposes_safe_diagnostic_fields(self):
        error_type = getattr(chat_service, "ChatInputLimitError", None)

        assert error_type is not None
        error = error_type(region="current_user", used=8193, limit=8192)
        assert isinstance(error, ChatServiceError)
        assert error.region == "current_user"
        assert error.used == 8193
        assert error.limit == 8192
        assert error.detail == (
            "Prompt input exceeds token budget: "
            "region=current_user used=8193 limit=8192"
        )
        assert str(error) == error.detail
        assert "ChatInputLimitError" in chat_service.__all__

    def test_runtime_converts_prompt_limit_error_and_preserves_cause(self):
        source_error = PromptInputLimitError(
            region="current_user",
            used=8193,
            limit=8192,
        )

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_BUILD_PROMPT, side_effect=source_error):
                with pytest.raises(ChatServiceError) as exc_info:
                    _chat_service(False).generate_chat_reply(
                        "miori", CONVERSATION_ID, "hello"
                    )

        error = exc_info.value
        assert error.__class__.__name__ == "ChatInputLimitError"
        assert error.region == source_error.region
        assert error.used == source_error.used
        assert error.limit == source_error.limit
        assert error.__cause__ is source_error

    def test_prompt_limit_error_marks_started_turn_failed_once(self):
        session = _RecordingHistorySession()
        service = _chat_service_with_history(session)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _BUILD_PROMPT,
                side_effect=PromptInputLimitError("current_user", 8193, 8192),
            ):
                with pytest.raises(ChatServiceError):
                    service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

        assert session.start_calls == ["hello"]
        assert session.complete_calls == []
        assert session.fail_calls == [session.started_turn]

    def test_prompt_limit_error_and_logs_do_not_contain_prompt_bodies(self, caplog):
        secrets = {
            "character": "SECRET_CHARACTER_PROMPT_86C2",
            "rag": "SECRET_RAG_BODY_41D7",
            "history_user": "SECRET_HISTORY_USER_5E09",
            "history_assistant": "SECRET_HISTORY_ASSISTANT_A13B",
            "current_user": "SECRET_CURRENT_USER_F742",
        }
        session = _RecordingHistorySession()
        session.restored_turns = (
            RestoredHistoryTurn(
                user_content=secrets["history_user"],
                assistant_content=secrets["history_assistant"],
                is_completed=True,
            ),
        )
        service = ChatService(
            ChatRuntimeConfig(
                rag_enabled=True,
                memory_policy=MagicMock(),
                prompt_config=_PROMPT_CONFIG,
                chroma_path=_CHROMA_PATH,
            ),
            _RecordingHistoryService(session),
            _runtime_dependencies(),
        )

        def reject_prompt(prompt_input):
            assert prompt_input.character.system_prompt == secrets["character"]
            assert prompt_input.rag.items[0].content.endswith(secrets["rag"])
            assert prompt_input.rag.items[0].raw_distance == 1.25
            history = tuple(prompt_input.history.newest_first_factory())
            assert history[0].user_content == secrets["history_user"]
            assert history[0].assistant_content == secrets["history_assistant"]
            assert prompt_input.current_user.content == secrets["current_user"]
            raise PromptInputLimitError("current_user", 8193, 8192)

        with patch(
            _LOAD_PERSONALITY,
            return_value=_character_card(secrets["character"]),
        ):
            with patch(
                _BUILD_AUGMENTED_SYSTEM_PROMPT,
                return_value=_rag_outcome(_rag_memory(secrets["rag"])),
            ):
                with patch(
                    _BUILD_PROMPT,
                    side_effect=reject_prompt,
                ):
                    with pytest.raises(ChatServiceError) as exc_info:
                        service.generate_chat_reply(
                            "miori",
                            CONVERSATION_ID,
                            secrets["current_user"],
                        )

        observed = "\n".join((str(exc_info.value), repr(exc_info.value), caplog.text))
        for secret in secrets.values():
            assert secret not in observed

    def test_infra_functions_are_not_public_api(self):
        assert not hasattr(chat_service, "load_personality")
        assert not hasattr(chat_service, "generate_response")
        assert not hasattr(chat_service, "resolved_memory_policy")
        assert not hasattr(chat_service, "build_augmented_system_prompt")
        assert not hasattr(chat_service, "record_user_memory_candidate")
        assert not hasattr(chat_service, "MemoryPolicy")
        assert not hasattr(chat_service, "BackgroundTaskQueue")
        assert not hasattr(chat_service, "_generate_chat_reply_for_runtime")
        assert not hasattr(chat_service, "_create_chat_session_for_runtime")
        assert not hasattr(chat_service, "_generate_chat_reply_with_memory_queue")
        assert not hasattr(chat_service, "_create_chat_session_with_memory_queue")
        assert not hasattr(chat_service, "configure_memory_task_queue")
        assert not hasattr(chat_service, "clear_memory_task_queue")
        assert not hasattr(chat_service, "_configured_memory_task_queue")
        assert not hasattr(chat_service, "_queue_lock")
        assert not hasattr(chat_service, "memory_task_queue_scope")
        assert not hasattr(chat_service, "_ThreadedMemoryTaskQueue")
        assert not hasattr(chat_service, "ChatRuntimeConfig")
        assert not hasattr(chat_service, "ChatService")
        assert not hasattr(chat_service, "ThreadPoolMemoryTaskQueue")
        assert not hasattr(chat_service, "create_chat_service")
        assert hasattr(chat_service, "generate_chat_reply")
        assert hasattr(chat_service, "create_chat_session")
        assert "generate_chat_reply" in chat_service.__all__
        assert "create_chat_session" in chat_service.__all__
        assert "ChatSession" not in chat_service.__all__
        assert "ChatService" not in chat_service.__all__
        assert "EventLoopChatTaskQueue" not in chat_service.__all__
        assert "ThreadPoolChatTaskQueue" not in chat_service.__all__
        assert "ThreadPoolMemoryTaskQueue" not in chat_service.__all__
        assert "create_chat_service" not in chat_service.__all__

    def test_normalizes_missing_character_error(self):
        with patch(_LOAD_PERSONALITY, side_effect=FileNotFoundError("missing")):
            with pytest.raises(CharacterNotFoundError) as exc_info:
                _chat_service(False).generate_chat_reply(
                    "unknown", CONVERSATION_ID, "hello"
                )

        assert exc_info.value.detail == "Character 'unknown' not found"

    def test_normalizes_llm_timeout_error(self):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.ReadTimeout("timeout")):
                with pytest.raises(ChatTimeoutError) as exc_info:
                    _chat_service(False).generate_chat_reply(
                        "miori", CONVERSATION_ID, "hello"
                    )

        assert exc_info.value.detail == "LLM request timed out"

    def test_normalizes_llm_backend_error(self):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.HTTPError("boom")):
                with pytest.raises(ChatBackendError) as exc_info:
                    _chat_service(False).generate_chat_reply(
                        "miori", CONVERSATION_ID, "hello"
                    )

        assert exc_info.value.detail == "LLM request failed"

    @pytest.mark.parametrize(
        ("source_error", "expected_error", "expected_detail"),
        [
            (
                httpx.ReadTimeout("timeout"),
                ChatTimeoutError,
                "LLM request timed out",
            ),
            (
                httpx.HTTPError("boom"),
                ChatBackendError,
                "LLM request failed",
            ),
        ],
    )
    def test_normalizes_prompt_token_count_transport_error(
        self,
        source_error: httpx.HTTPError,
        expected_error: type[ChatServiceError],
        expected_detail: str,
    ) -> None:
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                "app.llm.router.count_input_tokens",
                side_effect=source_error,
            ):
                with pytest.raises(expected_error) as exc_info:
                    _chat_service(False).generate_chat_reply(
                        "miori", CONVERSATION_ID, "hello"
                    )

        assert exc_info.value.detail == expected_detail
        assert exc_info.value.__cause__ is source_error

    def test_generate_reply_failure_marks_started_turn_failed_once(self):
        session = _RecordingHistorySession()
        service = _chat_service_with_history(session)
        original_error = RuntimeError("generation failed")

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=original_error):
                with pytest.raises(RuntimeError) as exc_info:
                    service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

        assert exc_info.value is original_error
        assert session.start_calls == ["hello"]
        assert session.complete_calls == []
        assert session.fail_calls == [session.started_turn]

    def test_complete_turn_failure_marks_started_turn_failed_once(self):
        original_error = RuntimeError("completion failed")
        session = _FailingCompleteHistorySession(original_error)
        service = _chat_service_with_history(session)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value="reply"):
                with pytest.raises(RuntimeError) as exc_info:
                    service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

        assert exc_info.value is original_error
        assert session.start_calls == ["hello"]
        assert session.complete_calls == [(session.started_turn, "reply")]
        assert session.fail_calls == [session.started_turn]

    def test_completed_persisted_turn_submits_identifier_only_formation_job(self):
        from dataclasses import asdict

        submitter = MagicMock()
        session = _RecordingHistorySession()
        service = ChatService(
            ChatRuntimeConfig(
                rag_enabled=False,
                memory_policy=None,
                prompt_config=_PROMPT_CONFIG,
                chroma_path=_CHROMA_PATH,
            ),
            _RecordingHistoryService(session),
            _runtime_dependencies(submitter),
        )

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value="reply"):
                service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

        submitter.submit.assert_called_once()
        assert asdict(submitter.submit.call_args.args[0]) == {
            "character_id": "miori",
            "conversation_id": CONVERSATION_ID,
            "turn_id": session.started_turn.turn_id,
        }

    def test_history_persistence_failure_never_submits_formation_job(self):
        submitter = MagicMock()
        session = _FailingCompleteHistorySession(RuntimeError("completion failed"))
        service = ChatService(
            ChatRuntimeConfig(
                rag_enabled=False,
                memory_policy=None,
                prompt_config=_PROMPT_CONFIG,
                chroma_path=_CHROMA_PATH,
            ),
            _RecordingHistoryService(session),
            _runtime_dependencies(submitter),
        )

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value="reply"):
                with pytest.raises(RuntimeError, match="completion failed"):
                    service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

        submitter.submit.assert_not_called()

    def test_privacy_skipped_turn_never_submits_formation_job(self):
        submitter = MagicMock()
        service = ChatService(
            ChatRuntimeConfig(
                rag_enabled=False,
                memory_policy=None,
                prompt_config=_PROMPT_CONFIG,
                chroma_path=_CHROMA_PATH,
            ),
            _RecordingHistoryService(_PrivacySkippedHistorySession()),
            _runtime_dependencies(submitter),
        )

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value="reply"):
                service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

        submitter.submit.assert_not_called()

    def test_cleanup_failure_does_not_replace_generate_reply_error(self):
        original_error = RuntimeError("generation failed")
        session = _FailingCleanupHistorySession(RuntimeError("cleanup failed"))
        service = _chat_service_with_history(session)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=original_error):
                with pytest.raises(RuntimeError) as exc_info:
                    service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

        assert exc_info.value is original_error
        assert session.complete_calls == []
        assert session.fail_calls == [session.started_turn]

    def test_cleanup_failure_does_not_replace_complete_turn_error(self):
        original_error = RuntimeError("completion failed")
        session = _FailingCompleteAndCleanupHistorySession(
            original_error,
            RuntimeError("cleanup failed"),
        )
        service = _chat_service_with_history(session)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value="reply"):
                with pytest.raises(RuntimeError) as exc_info:
                    service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

        assert exc_info.value is original_error
        assert session.complete_calls == [(session.started_turn, "reply")]
        assert session.fail_calls == [session.started_turn]

    def test_success_completes_started_turn_without_marking_it_failed(self):
        session = _RecordingHistorySession()
        service = _chat_service_with_history(session)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value="reply"):
                reply = service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

        assert _assistant_content(reply) == "reply"
        assert session.start_calls == ["hello"]
        assert session.complete_calls == [(session.started_turn, "reply")]
        assert session.fail_calls == []

    def test_successful_completion_remains_trackable_until_delivery(self):
        session = _RecordingHistorySession()
        service = _chat_service_with_history(session)

        async def run_session_flow() -> chat_service.ChatReply:
            with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                chat_session = await service.create_chat_session(
                    "miori",
                    CONVERSATION_ID,
                )
                with patch(_GENERATE_RESPONSE, return_value="reply"):
                    reply = chat_session.generate_reply("hello")
                chat_session.mark_delivery_failed(reply.turn_id)
                return reply

        reply = asyncio.run(run_session_flow())

        assert _assistant_content(reply) == "reply"
        assert session.complete_calls == [(session.started_turn, "reply")]
        assert session.fail_calls == [session.started_turn]

    def test_public_generate_chat_reply_delegates_to_configured_service(self):
        service = _chat_service(False)
        resolver = lambda: service
        _chat_runtime.register_default_chat_service_resolver(resolver)
        try:
            with patch(_LOAD_PERSONALITY, return_value=_character_card()) as mock_load:
                with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                    reply = chat_service.generate_chat_reply(
                        "miori", CONVERSATION_ID, "hello"
                    )
        finally:
            _chat_runtime.clear_default_chat_service_resolver(resolver)

        assert _assistant_content(reply) == "reply"
        mock_load.assert_called_once_with("miori")
        assert _generated_contents(mock_gen) == ["## 応答方針\n# prompt", "hello"]

    def test_public_create_chat_session_delegates_to_configured_service(self):
        async def run_session_flow():
            service = _chat_service(False)
            resolver = lambda: service
            _chat_runtime.register_default_chat_service_resolver(resolver)
            try:
                with patch(
                    _LOAD_PERSONALITY,
                    side_effect=[_character_card("# open"), _character_card()],
                ):
                    session = await chat_service.create_chat_session(
                        "miori", CONVERSATION_ID
                    )
                    with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                        reply = session.generate_reply("hello")
            finally:
                _chat_runtime.clear_default_chat_service_resolver(resolver)
            return reply, mock_gen

        reply, mock_gen = asyncio.run(run_session_flow())

        assert _assistant_content(reply) == "reply"
        assert _generated_contents(mock_gen) == ["## 応答方針\n# prompt", "hello"]

    def test_public_entrypoints_fail_fast_without_registered_service(self):
        with pytest.raises(ChatServiceError, match="resolver is not configured"):
            chat_service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

    def test_public_entrypoints_follow_registered_app_state_service(self):
        class StubSession:
            def generate_reply(self, message: str) -> chat_service.ChatReply:
                return persisted_reply(f"ws:{message}", CONVERSATION_ID)

            def mark_delivered(self, turn_id: UUID) -> None:
                return None

            def mark_delivery_failed(self, turn_id: UUID) -> None:
                return None

            def close(self) -> None:
                return None

        class StubChatService:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str]] = []

            def generate_chat_reply(
                self,
                character: str,
                conversation_id: UUID,
                message: str,
            ) -> chat_service.ChatReply:
                self.calls.append(("http", character, message))
                return persisted_reply(f"http:{message}", CONVERSATION_ID)

            async def create_chat_session(
                self,
                character: str,
                conversation_id: UUID,
            ) -> chat_service.ChatReplySession:
                self.calls.append(("ws-open", character, ""))
                return StubSession()

        first = StubChatService()
        second = StubChatService()
        state = {"service": first}

        def resolver():
            return state["service"]

        _chat_runtime.register_default_chat_service_resolver(resolver)
        try:
            assert (
                _assistant_content(
                    chat_service.generate_chat_reply("miori", CONVERSATION_ID, "hello")
                )
                == "http:hello"
            )
            state["service"] = second
            session = asyncio.run(
                chat_service.create_chat_session("miori", CONVERSATION_ID)
            )
        finally:
            _chat_runtime.clear_default_chat_service_resolver(resolver)

        reply = session.generate_reply("again")
        session.mark_delivered(reply.turn_id)
        session.mark_delivery_failed(reply.turn_id)
        session.close()

        assert _assistant_content(reply) == "ws:again"
        assert first.calls == [("http", "miori", "hello")]
        assert second.calls == [("ws-open", "miori", "")]

    def test_public_entrypoints_restore_previous_resolver_after_nested_clear(self):
        class StubChatService:
            def __init__(self, label: str) -> None:
                self.label = label

            def generate_chat_reply(
                self,
                character: str,
                conversation_id: UUID,
                message: str,
            ) -> chat_service.ChatReply:
                return persisted_reply(
                    f"{self.label}:{character}:{message}",
                    conversation_id,
                )

            async def create_chat_session(
                self,
                character: str,
                conversation_id: UUID,
            ):
                raise AssertionError("not used")

        first = StubChatService("first")
        second = StubChatService("second")
        first_resolver = lambda: first
        second_resolver = lambda: second

        _chat_runtime.register_default_chat_service_resolver(first_resolver)
        try:
            _chat_runtime.register_default_chat_service_resolver(second_resolver)
            try:
                assert (
                    _assistant_content(
                        chat_service.generate_chat_reply(
                            "miori", CONVERSATION_ID, "hello"
                        )
                    )
                    == "second:miori:hello"
                )
            finally:
                _chat_runtime.clear_default_chat_service_resolver(second_resolver)

            assert (
                _assistant_content(
                    chat_service.generate_chat_reply("miori", CONVERSATION_ID, "again")
                )
                == "first:miori:again"
            )
        finally:
            _chat_runtime.clear_default_chat_service_resolver(first_resolver)


class TestChatServiceRagContract:
    def test_rag_skip_continues_normal_reply_without_memory(self):
        policy = object()
        service = _chat_service(True, policy)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _BUILD_AUGMENTED_SYSTEM_PROMPT, return_value=_rag_outcome()
            ):
                with patch(_GENERATE_RESPONSE, return_value="reply") as generate:
                    reply = service.generate_chat_reply(
                        "miori", CONVERSATION_ID, "hello"
                    )

        assert _assistant_content(reply) == "reply"
        assert _generated_contents(generate) == ["## 応答方針\n# prompt", "hello"]

    def test_two_argument_reply_uses_rag_augmented_prompt_when_enabled(self):
        policy = object()
        base_prompt = "# prompt"
        service = _chat_service(True, policy)
        with patch(_LOAD_PERSONALITY, return_value=_character_card(base_prompt)):
            with patch(
                _BUILD_AUGMENTED_SYSTEM_PROMPT,
                return_value=_rag_outcome(_rag_memory("畑の話")),
            ) as mock_build:
                with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                    reply = service.generate_chat_reply(
                        "miori", CONVERSATION_ID, "hello"
                    )

        assert _assistant_content(reply) == "reply"
        mock_build.assert_called_once_with(
            "miori",
            "hello",
            policy,
            scanner=service._dependencies.privacy_scanner,
            classifier=service._dependencies.semantic_classifier,
            approved_repository=service._dependencies.approved_memory_repository,
            chroma_path=_CHROMA_PATH,
            now=ANY,
            timezone="Asia/Tokyo",
        )
        assert _generated_contents(mock_gen) == [
            "## 応答方針\n# prompt",
            "## 関連する記憶\n[2026-07-31T09:00:00+09:00 DAY] 畑の話",
            "hello",
        ]

    def test_memory_date_is_displayed_in_the_runtime_occurrence_timezone(self):
        policy = object()
        service = _chat_service(True, policy)
        memory = _rag_memory(
            "月境界の旅行",
            occurred_at="2026-07-31T15:30:00+00:00",
        )

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _BUILD_AUGMENTED_SYSTEM_PROMPT,
                return_value=_rag_outcome(memory),
            ):
                with patch(_GENERATE_RESPONSE, return_value="reply") as generate:
                    service.generate_chat_reply("miori", CONVERSATION_ID, "旅行の話")

        assert any(
            "[2026-08-01T00:30:00+09:00 DAY] 月境界の旅行" in content
            for content in _generated_contents(generate)
        )

    def test_success_logs_only_metadata_for_memories_selected_into_prompt(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        policy = object()
        memory_id = "00000000-0000-4000-8000-000000000043"
        private_body = "PRIVATE_MEMORY_BODY_4C62"
        private_query = "PRIVATE_QUERY_BODY_9A17"
        outcome = MagicMock()
        outcome.memories = (_rag_memory(private_body, memory_id),)
        outcome.no_match = False
        service = _chat_service(True, policy)
        caplog.set_level("INFO")

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT, return_value=outcome):
                with patch(_GENERATE_RESPONSE, return_value="reply"):
                    service.generate_chat_reply(
                        "miori",
                        CONVERSATION_ID,
                        private_query,
                    )

        assert memory_id in caplog.text
        assert "2026-07-31T00:00:00.000000Z" in caplog.text
        assert private_body not in caplog.text
        assert private_query not in caplog.text

    def test_failed_llm_response_does_not_log_prompt_memory_metadata(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        policy = object()
        memory_id = "00000000-0000-4000-8000-000000000044"
        service = _chat_service(True, policy)
        caplog.set_level("INFO")

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _BUILD_AUGMENTED_SYSTEM_PROMPT,
                return_value=_rag_outcome(_rag_memory("非公開本文", memory_id)),
            ):
                with patch(
                    _GENERATE_RESPONSE,
                    side_effect=httpx.HTTPError("synthetic failure"),
                ):
                    with pytest.raises(ChatBackendError):
                        service.generate_chat_reply(
                            "miori",
                            CONVERSATION_ID,
                            "非公開query",
                        )

        assert memory_id not in caplog.text
        assert "2026-07-31T00:00:00.000000Z" not in caplog.text

    def test_successful_temporal_no_match_adds_explicit_non_inference_instruction(
        self,
    ) -> None:
        policy = object()
        outcome = MagicMock()
        outcome.memories = ()
        outcome.no_match = True
        service = _chat_service(True, policy)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT, return_value=outcome):
                with patch(_GENERATE_RESPONSE, return_value="reply") as generate:
                    service.generate_chat_reply("miori", CONVERSATION_ID, "昨年3月の旅行")

        assert _generated_contents(generate) == [
            "## 応答方針\n# prompt",
            "## 関連する記憶\n指定された期間に該当する記憶はありません。推測で補完しないでください。",
            "昨年3月の旅行",
        ]

    def test_unknown_occurrence_is_injected_without_a_date_label(self) -> None:
        policy = object()
        outcome = MagicMock()
        outcome.memories = (_rag_memory("日付不明の旅行", occurred_at=None),)
        outcome.no_match = False
        service = _chat_service(True, policy)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT, return_value=outcome):
                with patch(_GENERATE_RESPONSE, return_value="reply") as generate:
                    service.generate_chat_reply("miori", CONVERSATION_ID, "旅行の話")

        assert _generated_contents(generate) == [
            "## 応答方針\n# prompt",
            "## 関連する記憶\n日付不明の旅行",
            "旅行の話",
        ]

    def test_rag_disabled_keeps_plain_prompt_without_memory_work(self):
        service = _chat_service(False)
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT) as mock_build:
                with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                    reply = service.generate_chat_reply(
                        "miori", CONVERSATION_ID, "hello"
                    )

        assert _assistant_content(reply) == "reply"
        mock_build.assert_not_called()
        assert _generated_contents(mock_gen) == ["## 応答方針\n# prompt", "hello"]

    def test_chat_session_uses_same_per_message_resolution_as_http_reply(self):
        policy = object()
        service = _chat_service(True, policy)

        async def run_session_flow():
            with patch(
                _LOAD_PERSONALITY,
                side_effect=[
                    _character_card("# open"),
                    _character_card("# prompt 1"),
                    _character_card("# prompt 2"),
                ],
            ):
                session = await service.create_chat_session("miori", CONVERSATION_ID)
                with patch(
                    _BUILD_AUGMENTED_SYSTEM_PROMPT,
                    side_effect=[
                        _rag_outcome(_rag_memory("memory 1", "memory-1")),
                        _rag_outcome(_rag_memory("memory 2", "memory-2")),
                    ],
                ) as mock_build:
                    with patch(
                        _GENERATE_RESPONSE,
                        side_effect=["reply 1", "reply 2"],
                    ) as mock_gen:
                        first_reply = session.generate_reply("hello")
                        second_reply = session.generate_reply("again")
            return (
                first_reply,
                second_reply,
                mock_build,
                mock_gen,
            )

        (
            first_reply,
            second_reply,
            mock_build,
            mock_gen,
        ) = asyncio.run(run_session_flow())

        assert _assistant_content(first_reply) == "reply 1"
        assert _assistant_content(second_reply) == "reply 2"
        assert mock_build.call_count == 2
        retrieval_dependencies = {
            "scanner": service._dependencies.privacy_scanner,
            "classifier": service._dependencies.semantic_classifier,
            "approved_repository": service._dependencies.approved_memory_repository,
            "chroma_path": _CHROMA_PATH,
            "now": ANY,
            "timezone": "Asia/Tokyo",
        }
        mock_build.assert_any_call("miori", "hello", policy, **retrieval_dependencies)
        mock_build.assert_any_call("miori", "again", policy, **retrieval_dependencies)
        assert mock_gen.call_count == 2

    def test_runtime_config_fails_fast_for_inconsistent_rag_policy(self):
        with pytest.raises(ValueError, match="memory policy is required"):
            _chat_service(True)
        with pytest.raises(ValueError, match="memory policy must be omitted"):
            _chat_service(False, object())
