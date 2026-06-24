import asyncio
import inspect

import httpx
import pytest
from unittest.mock import patch

from app import chat_service
from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatTimeoutError,
    create_chat_session,
    generate_chat_reply,
)


_LOAD_PERSONALITY = "app.chat_service._character_loader.load_personality"
_GENERATE_RESPONSE = "app.chat_service._llm_router.generate_response"
_BUILD_AUGMENTED_SYSTEM_PROMPT = (
    "app.chat_service._rag_service.build_augmented_system_prompt"
)
_RECORD_CHAT_TURN = "app.chat_service._rag_service.record_chat_turn"
_RESOLVED_MEMORY_POLICY = "app.chat_service._memory_policy.resolved_memory_policy"


class TestChatServiceErrorContract:
    def test_infra_functions_are_not_public_api(self):
        assert not hasattr(chat_service, "load_personality")
        assert not hasattr(chat_service, "generate_response")
        assert not hasattr(chat_service, "resolved_memory_policy")
        assert not hasattr(chat_service, "build_augmented_system_prompt")
        assert not hasattr(chat_service, "record_chat_turn")
        assert not hasattr(chat_service, "MemoryPolicy")
        assert not hasattr(chat_service, "BackgroundTaskQueue")
        assert "ChatSession" not in chat_service.__all__
        assert "EventLoopChatTaskQueue" not in chat_service.__all__
        assert "ThreadPoolChatTaskQueue" not in chat_service.__all__

    def test_create_chat_session_does_not_require_event_loop_argument(self):
        signature = inspect.signature(chat_service.create_chat_session)
        assert list(signature.parameters) == ["character"]
        assert signature.return_annotation is chat_service.ChatReplySession

    def test_generate_chat_reply_does_not_expose_task_queue_argument(self):
        signature = inspect.signature(chat_service.generate_chat_reply)
        assert list(signature.parameters) == ["character", "message"]
        assert "_ChatTaskQueue" not in str(signature)
        assert "_ThreadPoolChatTaskQueue" not in str(signature)

    def test_normalizes_missing_character_error(self):
        with patch(_LOAD_PERSONALITY, side_effect=FileNotFoundError("missing")):
            with pytest.raises(CharacterNotFoundError) as exc_info:
                generate_chat_reply("unknown", "hello")

        assert exc_info.value.detail == "Character 'unknown' not found"

    def test_normalizes_llm_timeout_error(self):
        with patch(_LOAD_PERSONALITY, return_value="# prompt"):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.ReadTimeout("timeout")):
                with pytest.raises(ChatTimeoutError) as exc_info:
                    generate_chat_reply("miori", "hello")

        assert exc_info.value.detail == "LLM request timed out"

    def test_normalizes_llm_backend_error(self):
        with patch(_LOAD_PERSONALITY, return_value="# prompt"):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.HTTPError("boom")):
                with pytest.raises(ChatBackendError) as exc_info:
                    generate_chat_reply("miori", "hello")

        assert exc_info.value.detail == "LLM request failed"


class TestChatServiceRagContract:
    def test_two_argument_reply_uses_rag_augmented_prompt_when_enabled(self):
        policy = object()
        base_prompt = "# prompt"
        augmented_prompt = "# prompt\n\n過去の記憶:\n畑の話"

        with patch.dict("os.environ", {"RAG_ENABLED": "true"}):
            with patch(_RESOLVED_MEMORY_POLICY, return_value=policy):
                with patch(_LOAD_PERSONALITY, return_value=base_prompt):
                    with patch(
                        _BUILD_AUGMENTED_SYSTEM_PROMPT,
                        return_value=augmented_prompt,
                    ) as mock_build:
                        with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                            with patch(_RECORD_CHAT_TURN):
                                reply = generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_build.assert_called_once_with("miori", "hello", base_prompt, policy)
        mock_gen.assert_called_once_with(augmented_prompt, "hello")

    def test_two_argument_reply_records_turn_when_rag_enabled(self):
        policy = object()

        with patch.dict("os.environ", {"RAG_ENABLED": "true"}):
            with patch(_RESOLVED_MEMORY_POLICY, return_value=policy):
                with patch(_LOAD_PERSONALITY, return_value="# prompt"):
                    with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT, return_value="# augmented"):
                        with patch(_GENERATE_RESPONSE, return_value="reply"):
                            with patch(_RECORD_CHAT_TURN) as mock_record:
                                reply = generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_record.assert_called_once()
        args, _kwargs = mock_record.call_args
        assert args[:3] == ("miori", "hello", "reply")
        assert args[4] is policy

    def test_rag_disabled_keeps_plain_prompt_without_memory_work(self):
        with patch.dict("os.environ", {"RAG_ENABLED": "false"}):
            with patch(_RESOLVED_MEMORY_POLICY) as mock_policy:
                with patch(_LOAD_PERSONALITY, return_value="# prompt"):
                    with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT) as mock_build:
                        with patch(_GENERATE_RESPONSE, return_value="reply") as mock_gen:
                            with patch(_RECORD_CHAT_TURN) as mock_record:
                                reply = generate_chat_reply("miori", "hello")

        assert reply == "reply"
        mock_policy.assert_not_called()
        mock_build.assert_not_called()
        mock_gen.assert_called_once_with("# prompt", "hello")
        mock_record.assert_not_called()

    def test_chat_session_resolves_rag_context_once_at_creation(self):
        policy = object()
        base_prompt = "# prompt"

        async def run_session_flow():
            with patch.dict("os.environ", {"RAG_ENABLED": "true"}):
                with patch(_RESOLVED_MEMORY_POLICY, return_value=policy) as mock_policy:
                    with patch(_LOAD_PERSONALITY, return_value=base_prompt):
                        session = await create_chat_session("miori")
            with patch.dict("os.environ", {"RAG_ENABLED": "false"}):
                with patch(
                    _BUILD_AUGMENTED_SYSTEM_PROMPT,
                    side_effect=["# augmented 1", "# augmented 2"],
                ) as mock_build:
                    with patch(
                        _GENERATE_RESPONSE,
                        side_effect=["reply 1", "reply 2"],
                    ) as mock_gen:
                        with patch(_RECORD_CHAT_TURN) as mock_record:
                            first_reply = session.generate_reply("hello")
                            second_reply = session.generate_reply("again")
            return (
                first_reply,
                second_reply,
                mock_policy,
                mock_build,
                mock_gen,
                mock_record,
            )

        (
            first_reply,
            second_reply,
            mock_policy,
            mock_build,
            mock_gen,
            mock_record,
        ) = asyncio.run(run_session_flow())

        assert first_reply == "reply 1"
        assert second_reply == "reply 2"
        mock_policy.assert_called_once_with()
        assert mock_build.call_count == 2
        mock_build.assert_any_call("miori", "hello", base_prompt, policy)
        mock_build.assert_any_call("miori", "again", base_prompt, policy)
        mock_gen.assert_any_call("# augmented 1", "hello")
        mock_gen.assert_any_call("# augmented 2", "again")
        assert mock_record.call_count == 2
