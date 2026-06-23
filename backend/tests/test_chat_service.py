import httpx
import pytest
from unittest.mock import patch

from app import chat_service
from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatTimeoutError,
    generate_chat_reply,
)


_LOAD_PERSONALITY = "app.chat_service._character_loader.load_personality"
_GENERATE_RESPONSE = "app.chat_service._llm_router.generate_response"


class TestChatServiceErrorContract:
    def test_infra_functions_are_not_public_api(self):
        assert not hasattr(chat_service, "load_personality")
        assert not hasattr(chat_service, "generate_response")

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
