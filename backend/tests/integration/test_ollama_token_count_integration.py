import httpx

from app.llm.ollama_client import OllamaClient
from app.llm.ollama_config import ollama_endpoint, ollama_timeout
from app.prompting import PromptMessage, PromptRole


def test_real_ollama_counter_matches_chat_prompt_evaluation() -> None:
    messages = (
        PromptMessage(PromptRole.SYSTEM, "あなたは光織です。"),
        PromptMessage(PromptRole.USER, "短く挨拶してください。"),
    )
    expected_response = httpx.post(
        ollama_endpoint("/api/chat"),
        json={
            "model": "gemma4:e4b",
            "stream": False,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in messages
            ],
            "options": {"num_ctx": 8192, "num_predict": 1},
        },
        timeout=ollama_timeout(),
    )
    expected_response.raise_for_status()
    expected = expected_response.json()["prompt_eval_count"]

    actual = OllamaClient(
        model_name="gemma4:e4b",
        context_tokens=8192,
    ).count_input_tokens(messages)

    assert actual == expected
