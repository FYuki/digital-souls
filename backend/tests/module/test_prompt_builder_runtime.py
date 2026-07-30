import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

class _CollectingTaskQueue:
    def add_task(
        self,
        func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        raise AssertionError("RAG無効時にmemory taskを登録してはいけない")


def _card_data() -> dict[str, object]:
    repository_root = Path(__file__).resolve().parents[3]
    raw_card = json.loads(
        (repository_root / "characters" / "miori" / "miori.card.json").read_text(
            encoding="utf-8"
        )
    )
    data = raw_card["data"]
    assert isinstance(data, dict)
    return data


def _generate_reply_through_runtime(
    current_user_text: str,
) -> tuple[str, dict[str, object]]:
    from app._chat_runtime import ChatRuntimeConfig, ChatService

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"message": {"content": "応答"}}
    with patch("app.llm.ollama_client.httpx.post", return_value=response) as post:
        result = ChatService(
            ChatRuntimeConfig(rag_enabled=False, memory_policy=None),
            _CollectingTaskQueue(),
        ).generate_chat_reply("miori", current_user_text)
    payload = cast(dict[str, object], post.call_args.kwargs["json"])
    return result, payload


def test_runtime_propagates_card_prompt_through_router_to_ollama_payload() -> None:
    data = _card_data()
    current_user_text = "統合経路の現在発言"

    result, payload = _generate_reply_through_runtime(current_user_text)

    character_block = "\n\n".join(
        str(data[field])
        for field in (
            "description",
            "personality",
            "scenario",
            "system_prompt",
            "mes_example",
        )
    )
    assert result == "応答"
    assert payload["messages"] == [
        {"role": "system", "content": character_block},
        {"role": "user", "content": current_user_text},
        {
            "role": "system",
            "content": data["post_history_instructions"],
        },
    ]


def test_runtime_logs_no_card_or_current_user_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = _card_data()
    current_user_text = "RUNTIME_CURRENT_USER_LOG_SENTINEL"
    caplog.set_level(logging.DEBUG)

    _generate_reply_through_runtime(current_user_text)

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert current_user_text not in rendered
    for field in ("description", "personality", "scenario", "mes_example"):
        assert str(data[field]) not in rendered
