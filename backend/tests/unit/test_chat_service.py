import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
import pytest

from app import chat_service
from app import _chat_runtime
from app._chat_runtime import (
    ChatRuntimeConfig,
    ChatService,
    ThreadPoolMemoryTaskQueue,
)
from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatServiceError,
    ChatTimeoutError,
)
from app.conversation_history.prompt_history import RestoredHistoryTurn
from app.conversation_history.service import StartedHistoryTurn
from app.memory.chroma_store import MemorySearchResult
from app.prompting import CharacterPrompt, PromptInputLimitError
from app.prompting.config import PromptRuntimeConfig


_LOAD_PERSONALITY = "app._chat_runtime._character_loader.load_character_card"
_GENERATE_RESPONSE = "app._chat_runtime._llm_router.generate_response"
_BUILD_AUGMENTED_SYSTEM_PROMPT = (
    "app._chat_runtime._rag_service.retrieve_prompt_memories"
)
_RECORD_USER_MEMORY_CANDIDATE = (
    "app._chat_runtime._rag_service.record_user_memory_candidate"
)
_BUILD_PROMPT = "app.chat_prompt.PromptBuilder.build"
_PROMPT_CONFIG = PromptRuntimeConfig(10, 4096, 8192, 4096, 32768)


@pytest.fixture(autouse=True)
def _formal_token_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _chat_runtime._llm_router,
        "count_input_tokens",
        lambda messages: len(messages),
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


def _rag_memory(content: str) -> MemorySearchResult:
    return MemorySearchResult(
        content=content,
        timestamp="2026-07-31T00:00:00+00:00",
        role="user",
    )


def _generated_contents(generate: MagicMock) -> list[str]:
    prompt = generate.call_args.args[0]
    return [message.content for message in prompt.messages]


class _CollectingTaskQueue:
    def __init__(self) -> None:
        self.tasks: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def add_task(self, func, *args, **kwargs) -> None:
        self.tasks.append((func, args, kwargs))


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
    ) -> None:
        return None

    def fail_turn(self, started_turn: StartedHistoryTurn) -> None:
        return None

    def prompt_turns(self, *, max_completed_turns: int, page_size: int):
        return iter(())


class _IgnoringHistoryService:
    def open_session(self, character_id: str) -> _IgnoringHistorySession:
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
    ) -> None:
        self.complete_calls.append((started_turn, assistant_content))

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
    ) -> None:
        super().complete_turn(started_turn, assistant_content)
        raise self.error


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

    def open_session(self, character_id: str) -> _RecordingHistorySession:
        return self.session


def _chat_service(rag_enabled: bool, policy=None) -> ChatService:
    privacy_scanner = MagicMock() if rag_enabled else None
    return ChatService(
        ChatRuntimeConfig(
            rag_enabled=rag_enabled,
            memory_policy=policy,
            privacy_scanner=privacy_scanner,
            prompt_config=_PROMPT_CONFIG,
        ),
        _CollectingTaskQueue(),
        _IgnoringHistoryService(),
    )


def _chat_service_with_history(session: _RecordingHistorySession) -> ChatService:
    return ChatService(
        ChatRuntimeConfig(
            rag_enabled=False,
            memory_policy=None,
            privacy_scanner=None,
            prompt_config=_PROMPT_CONFIG,
        ),
        _CollectingTaskQueue(),
        _RecordingHistoryService(session),
    )


class TestChatServiceErrorContract:
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
                    _chat_service(False).generate_chat_reply("miori", "hello")

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
                    service.generate_chat_reply("miori", "hello")

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
                privacy_scanner=MagicMock(),
                prompt_config=_PROMPT_CONFIG,
            ),
            _CollectingTaskQueue(),
            _RecordingHistoryService(session),
        )

        def reject_prompt(prompt_input):
            assert prompt_input.character.system_prompt == secrets["character"]
            assert prompt_input.rag.items[0].content.endswith(secrets["rag"])
            assert prompt_input.history.turns[0].user_content == secrets[
                "history_user"
            ]
            assert prompt_input.history.turns[0].assistant_content == secrets[
                "history_assistant"
            ]
            assert prompt_input.current_user.content == secrets["current_user"]
            raise PromptInputLimitError("current_user", 8193, 8192)

        with patch(
            _LOAD_PERSONALITY,
            return_value=_character_card(secrets["character"]),
        ):
            with patch(
                _BUILD_AUGMENTED_SYSTEM_PROMPT,
                return_value=[_rag_memory(secrets["rag"])],
            ):
                with patch(
                    _BUILD_PROMPT,
                    side_effect=reject_prompt,
                ):
                    with pytest.raises(ChatServiceError) as exc_info:
                        service.generate_chat_reply(
                            "miori",
                            secrets["current_user"],
                        )

        observed = "\n".join(
            (str(exc_info.value), repr(exc_info.value), caplog.text)
        )
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
                _chat_service(False).generate_chat_reply("unknown", "hello")

        assert exc_info.value.detail == "Character 'unknown' not found"

    def test_normalizes_llm_timeout_error(self):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.ReadTimeout("timeout")):
                with pytest.raises(ChatTimeoutError) as exc_info:
                    _chat_service(False).generate_chat_reply("miori", "hello")

        assert exc_info.value.detail == "LLM request timed out"

    def test_normalizes_llm_backend_error(self):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.HTTPError("boom")):
                with pytest.raises(ChatBackendError) as exc_info:
                    _chat_service(False).generate_chat_reply("miori", "hello")

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
                "app._chat_runtime._llm_router.count_input_tokens",
                side_effect=source_error,
            ):
                with pytest.raises(expected_error) as exc_info:
                    _chat_service(False).generate_chat_reply("miori", "hello")

        assert exc_info.value.detail == expected_detail
        assert exc_info.value.__cause__ is source_error

    def test_generate_reply_failure_marks_started_turn_failed_once(self):
        session = _RecordingHistorySession()
        service = _chat_service_with_history(session)
        original_error = RuntimeError("generation failed")

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=original_error):
                with pytest.raises(RuntimeError) as exc_info:
                    service.generate_chat_reply("miori", "hello")

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
                    service.generate_chat_reply("miori", "hello")

        assert exc_info.value is original_error
        assert session.start_calls == ["hello"]
        assert session.complete_calls == [(session.started_turn, "reply")]
        assert session.fail_calls == [session.started_turn]

    def test_cleanup_failure_does_not_replace_generate_reply_error(self):
        original_error = RuntimeError("generation failed")
        session = _FailingCleanupHistorySession(RuntimeError("cleanup failed"))
        service = _chat_service_with_history(session)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=original_error):
                with pytest.raises(RuntimeError) as exc_info:
                    service.generate_chat_reply("miori", "hello")

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
                    service.generate_chat_reply("miori", "hello")

        assert exc_info.value is original_error
        assert session.complete_calls == [(session.started_turn, "reply")]
        assert session.fail_calls == [session.started_turn]

    def test_success_completes_started_turn_without_marking_it_failed(self):
        session = _RecordingHistorySession()
        service = _chat_service_with_history(session)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value="reply"):
                reply = service.generate_chat_reply("miori", "hello")

        assert reply == "reply"
        assert session.start_calls == ["hello"]
        assert session.complete_calls == [(session.started_turn, "reply")]
        assert session.fail_calls == []

    def test_public_generate_chat_reply_delegates_to_configured_service(self):
        service = _chat_service(False)
        resolver = lambda: service
        _chat_runtime.register_default_chat_service_resolver(resolver)
        try:
            with patch(_LOAD_PERSONALITY, return_value=_character_card()) as mock_load:
                with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                    reply = chat_service.generate_chat_reply("miori", "hello")
        finally:
            _chat_runtime.clear_default_chat_service_resolver(resolver)

        assert reply == "reply"
        mock_load.assert_called_once_with("miori")
        assert _generated_contents(mock_gen) == ["## 応答方針\n# prompt", "hello"]

    def test_public_create_chat_session_delegates_to_configured_service(self):
        async def run_session_flow():
            service = _chat_service(False)
            resolver = lambda: service
            _chat_runtime.register_default_chat_service_resolver(resolver)
            try:
                with patch(_LOAD_PERSONALITY, side_effect=[_character_card("# open"), _character_card()]):
                    session = await chat_service.create_chat_session("miori")
                    with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                        reply = session.generate_reply("hello")
            finally:
                _chat_runtime.clear_default_chat_service_resolver(resolver)
            return reply, mock_gen

        reply, mock_gen = asyncio.run(run_session_flow())

        assert reply == "reply"
        assert _generated_contents(mock_gen) == ["## 応答方針\n# prompt", "hello"]

    def test_public_entrypoints_fail_fast_without_registered_service(self):
        with pytest.raises(ChatServiceError, match="resolver is not configured"):
            chat_service.generate_chat_reply("miori", "hello")

    def test_public_entrypoints_follow_registered_app_state_service(self):
        class StubSession:
            def generate_reply(self, message: str) -> str:
                return f"ws:{message}"

        class StubChatService:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str]] = []

            def generate_chat_reply(self, character: str, message: str) -> str:
                self.calls.append(("http", character, message))
                return f"http:{message}"

            async def create_chat_session(self, character: str):
                self.calls.append(("ws-open", character, ""))
                return StubSession()

        first = StubChatService()
        second = StubChatService()
        state = {"service": first}

        def resolver():
            return state["service"]

        _chat_runtime.register_default_chat_service_resolver(resolver)
        try:
            assert chat_service.generate_chat_reply("miori", "hello") == "http:hello"
            state["service"] = second
            session = asyncio.run(chat_service.create_chat_session("miori"))
        finally:
            _chat_runtime.clear_default_chat_service_resolver(resolver)

        assert session.generate_reply("again") == "ws:again"
        assert first.calls == [("http", "miori", "hello")]
        assert second.calls == [("ws-open", "miori", "")]

    def test_public_entrypoints_restore_previous_resolver_after_nested_clear(self):
        class StubChatService:
            def __init__(self, label: str) -> None:
                self.label = label

            def generate_chat_reply(self, character: str, message: str) -> str:
                return f"{self.label}:{character}:{message}"

            async def create_chat_session(self, character: str):
                raise AssertionError("not used")

        first = StubChatService("first")
        second = StubChatService("second")
        first_resolver = lambda: first
        second_resolver = lambda: second

        _chat_runtime.register_default_chat_service_resolver(first_resolver)
        try:
            _chat_runtime.register_default_chat_service_resolver(second_resolver)
            try:
                assert chat_service.generate_chat_reply("miori", "hello") == (
                    "second:miori:hello"
                )
            finally:
                _chat_runtime.clear_default_chat_service_resolver(second_resolver)

            assert chat_service.generate_chat_reply("miori", "again") == (
                "first:miori:again"
            )
        finally:
            _chat_runtime.clear_default_chat_service_resolver(first_resolver)


class TestChatServiceRagContract:
    def test_two_argument_reply_uses_rag_augmented_prompt_when_enabled(self):
        policy = object()
        base_prompt = "# prompt"
        service = _chat_service(True, policy)
        with patch(_LOAD_PERSONALITY, return_value=_character_card(base_prompt)):
            with patch(
                _BUILD_AUGMENTED_SYSTEM_PROMPT,
                return_value=(_rag_memory("畑の話"),),
            ) as mock_build:
                with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                    with patch(_RECORD_USER_MEMORY_CANDIDATE):
                        reply = service.generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_build.assert_called_once_with("miori", "hello", policy)
        assert _generated_contents(mock_gen) == [
            "## 応答方針\n# prompt",
            "## 関連する記憶\n[2026-07-31T00:00:00+00:00] (user) 畑の話",
            "hello",
        ]

    def test_two_argument_reply_records_user_memory_candidate_when_rag_enabled(self):
        policy = object()

        service = _chat_service(True, policy)
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT, return_value=()):
                with patch(_GENERATE_RESPONSE, return_value="reply"):
                    with patch(_RECORD_USER_MEMORY_CANDIDATE) as mock_record:
                        reply = service.generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_record.assert_called_once()
        args, kwargs = mock_record.call_args
        assert args[:2] == ("miori", "hello")
        assert args[2] is policy
        assert hasattr(args[3], "add_task")
        assert hasattr(kwargs["privacy_scanner"], "scan")

    def test_two_argument_reply_uses_shared_memory_queue_when_rag_enabled(self):
        policy = object()

        service = _chat_service(True, policy)
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT, return_value=()):
                with patch(_GENERATE_RESPONSE, return_value="reply"):
                    with patch(_RECORD_USER_MEMORY_CANDIDATE) as mock_record:
                        reply = service.generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_record.assert_called_once()
        assert hasattr(mock_record.call_args.args[3], "add_task")
        assert hasattr(mock_record.call_args.kwargs["privacy_scanner"], "scan")

    def test_rag_disabled_keeps_plain_prompt_without_memory_work(self):
        service = _chat_service(False)
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT) as mock_build:
                with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                    with patch(_RECORD_USER_MEMORY_CANDIDATE) as mock_record:
                        reply = service.generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_build.assert_not_called()
        assert _generated_contents(mock_gen) == ["## 応答方針\n# prompt", "hello"]
        mock_record.assert_not_called()

    def test_chat_session_uses_same_per_message_resolution_as_http_reply(self):
        policy = object()
        service = _chat_service(True, policy)

        async def run_session_flow():
            with patch(_LOAD_PERSONALITY, side_effect=[_character_card("# open"), _character_card("# prompt 1"), _character_card("# prompt 2")]):
                session = await service.create_chat_session("miori")
                with patch(
                    _BUILD_AUGMENTED_SYSTEM_PROMPT,
                    side_effect=[
                        (_rag_memory("memory 1"),),
                        (_rag_memory("memory 2"),),
                    ],
                ) as mock_build:
                    with patch(
                        _GENERATE_RESPONSE,
                        side_effect=["reply 1", "reply 2"],
                    ) as mock_gen:
                        with patch(_RECORD_USER_MEMORY_CANDIDATE) as mock_record:
                            first_reply = session.generate_reply("hello")
                            second_reply = session.generate_reply("again")
            return (
                first_reply,
                second_reply,
                mock_build,
                mock_gen,
                mock_record,
            )

        (
            first_reply,
            second_reply,
            mock_build,
            mock_gen,
            mock_record,
        ) = asyncio.run(run_session_flow())

        assert first_reply == "reply 1"
        assert second_reply == "reply 2"
        assert mock_build.call_count == 2
        mock_build.assert_any_call("miori", "hello", policy)
        mock_build.assert_any_call("miori", "again", policy)
        assert mock_gen.call_count == 2
        assert mock_record.call_count == 2
        assert [call.args[:2] for call in mock_record.call_args_list] == [
            ("miori", "hello"),
            ("miori", "again"),
        ]
        for call in mock_record.call_args_list:
            assert hasattr(call.args[3], "add_task")
            assert hasattr(call.kwargs["privacy_scanner"], "scan")

    def test_runtime_config_fails_fast_for_inconsistent_rag_policy(self):
        with pytest.raises(ValueError, match="memory policy is required"):
            _chat_service(True)
        with pytest.raises(ValueError, match="memory policy must be omitted"):
            _chat_service(False, object())
        with pytest.raises(ValueError, match="privacy scanner is required"):
            ChatService(
                ChatRuntimeConfig(
                    rag_enabled=True,
                    memory_policy=object(),
                    privacy_scanner=None,
                    prompt_config=_PROMPT_CONFIG,
                ),
                _CollectingTaskQueue(),
                _IgnoringHistoryService(),
            )
        with pytest.raises(ValueError, match="privacy scanner must be omitted"):
            ChatService(
                ChatRuntimeConfig(
                    rag_enabled=False,
                    memory_policy=None,
                    privacy_scanner=MagicMock(),
                    prompt_config=_PROMPT_CONFIG,
                ),
                _CollectingTaskQueue(),
                _IgnoringHistoryService(),
            )

    def test_thread_pool_memory_task_queue_shutdown_waits_for_pending_tasks(self):
        task_started = threading.Event()
        release_task = threading.Event()
        completed = []

        def task() -> None:
            task_started.set()
            release_task.wait(timeout=5)
            completed.append("done")

        queue = ThreadPoolMemoryTaskQueue(
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-rag-memory")
        )
        queue.add_task(task)
        assert task_started.wait(timeout=5)
        release_task.set()
        queue.shutdown()

        assert completed == ["done"]
