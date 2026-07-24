import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

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


_LOAD_PERSONALITY = "app._chat_runtime._character_loader.load_personality"
_GENERATE_RESPONSE = "app._chat_runtime._llm_router.generate_response"
_BUILD_AUGMENTED_SYSTEM_PROMPT = (
    "app._chat_runtime._rag_service.build_augmented_system_prompt"
)
_RECORD_USER_MEMORY_CANDIDATE = (
    "app._chat_runtime._rag_service.record_user_memory_candidate"
)
_RESOLVED_MEMORY_POLICY = "app._chat_runtime.resolved_memory_policy"


class _CollectingTaskQueue:
    def __init__(self) -> None:
        self.tasks: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def add_task(self, func, *args, **kwargs) -> None:
        self.tasks.append((func, args, kwargs))


def _chat_service(rag_enabled: bool, policy=None) -> ChatService:
    return ChatService(
        ChatRuntimeConfig(
            rag_enabled=rag_enabled,
            memory_policy=policy,
        ),
        _CollectingTaskQueue(),
    )


class TestChatServiceErrorContract:
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
        with patch(_LOAD_PERSONALITY, return_value="# prompt"):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.ReadTimeout("timeout")):
                with pytest.raises(ChatTimeoutError) as exc_info:
                    _chat_service(False).generate_chat_reply("miori", "hello")

        assert exc_info.value.detail == "LLM request timed out"

    def test_normalizes_llm_backend_error(self):
        with patch(_LOAD_PERSONALITY, return_value="# prompt"):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.HTTPError("boom")):
                with pytest.raises(ChatBackendError) as exc_info:
                    _chat_service(False).generate_chat_reply("miori", "hello")

        assert exc_info.value.detail == "LLM request failed"

    def test_public_generate_chat_reply_delegates_to_configured_service(self):
        service = _chat_service(False)
        resolver = lambda: service
        _chat_runtime.register_default_chat_service_resolver(resolver)
        try:
            with patch(_LOAD_PERSONALITY, return_value="# prompt") as mock_load:
                with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                    reply = chat_service.generate_chat_reply("miori", "hello")
        finally:
            _chat_runtime.clear_default_chat_service_resolver(resolver)

        assert reply == "reply"
        mock_load.assert_called_once_with("miori")
        mock_gen.assert_called_once_with("# prompt", "hello")

    def test_public_create_chat_session_delegates_to_configured_service(self):
        async def run_session_flow():
            service = _chat_service(False)
            resolver = lambda: service
            _chat_runtime.register_default_chat_service_resolver(resolver)
            try:
                with patch(_LOAD_PERSONALITY, side_effect=["# open", "# prompt"]):
                    session = await chat_service.create_chat_session("miori")
                    with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                        reply = session.generate_reply("hello")
            finally:
                _chat_runtime.clear_default_chat_service_resolver(resolver)
            return reply, mock_gen

        reply, mock_gen = asyncio.run(run_session_flow())

        assert reply == "reply"
        mock_gen.assert_called_once_with("# prompt", "hello")

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
        augmented_prompt = "# prompt\n\n過去の記憶:\n畑の話"

        service = _chat_service(True, policy)
        with patch(_LOAD_PERSONALITY, return_value=base_prompt):
            with patch(
                _BUILD_AUGMENTED_SYSTEM_PROMPT,
                return_value=augmented_prompt,
            ) as mock_build:
                with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                    with patch(_RECORD_USER_MEMORY_CANDIDATE):
                        reply = service.generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_build.assert_called_once_with("miori", "hello", base_prompt, policy)
        mock_gen.assert_called_once_with(augmented_prompt, "hello")

    def test_two_argument_reply_records_user_memory_candidate_when_rag_enabled(self):
        policy = object()

        service = _chat_service(True, policy)
        with patch(_LOAD_PERSONALITY, return_value="# prompt"):
            with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT, return_value="# augmented"):
                with patch(_GENERATE_RESPONSE, return_value="reply"):
                    with patch(_RECORD_USER_MEMORY_CANDIDATE) as mock_record:
                        reply = service.generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_record.assert_called_once()
        args, _kwargs = mock_record.call_args
        assert args[:2] == ("miori", "hello")
        assert args[2] is policy
        assert hasattr(args[3], "add_task")

    def test_two_argument_reply_uses_shared_memory_queue_when_rag_enabled(self):
        policy = object()

        service = _chat_service(True, policy)
        with patch(_LOAD_PERSONALITY, return_value="# prompt"):
            with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT, return_value="# prompt"):
                with patch(_GENERATE_RESPONSE, return_value="reply"):
                    with patch(_RECORD_USER_MEMORY_CANDIDATE) as mock_record:
                        reply = service.generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_record.assert_called_once()
        assert hasattr(mock_record.call_args.args[3], "add_task")

    def test_rag_disabled_keeps_plain_prompt_without_memory_work(self):
        service = _chat_service(False)
        with patch(_RESOLVED_MEMORY_POLICY) as mock_policy:
            with patch(_LOAD_PERSONALITY, return_value="# prompt"):
                with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT) as mock_build:
                    with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                        with patch(_RECORD_USER_MEMORY_CANDIDATE) as mock_record:
                            reply = service.generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_policy.assert_not_called()
        mock_build.assert_not_called()
        mock_gen.assert_called_once_with("# prompt", "hello")
        mock_record.assert_not_called()

    def test_chat_session_uses_same_per_message_resolution_as_http_reply(self):
        policy = object()
        service = _chat_service(True, policy)

        async def run_session_flow():
            with patch(_LOAD_PERSONALITY, side_effect=["# open", "# prompt 1", "# prompt 2"]):
                session = await service.create_chat_session("miori")
                with patch(
                    _BUILD_AUGMENTED_SYSTEM_PROMPT,
                    side_effect=["# augmented 1", "# augmented 2"],
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
        mock_build.assert_any_call("miori", "hello", "# prompt 1", policy)
        mock_build.assert_any_call("miori", "again", "# prompt 2", policy)
        mock_gen.assert_any_call("# augmented 1", "hello")
        mock_gen.assert_any_call("# augmented 2", "again")
        assert mock_record.call_count == 2
        assert [call.args[:2] for call in mock_record.call_args_list] == [
            ("miori", "hello"),
            ("miori", "again"),
        ]
        for call in mock_record.call_args_list:
            assert hasattr(call.args[3], "add_task")

    def test_runtime_config_fails_fast_for_inconsistent_rag_policy(self):
        with pytest.raises(ValueError, match="memory policy is required"):
            _chat_service(True)
        with pytest.raises(ValueError, match="memory policy must be omitted"):
            _chat_service(False, object())

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
