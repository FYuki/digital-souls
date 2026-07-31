import pytest

from app.llm import router


@pytest.fixture(autouse=True)
def mock_provider_token_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        router,
        "count_input_tokens",
        lambda messages: len(messages),
    )
