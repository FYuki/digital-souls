import importlib
from unittest.mock import patch

import pytest
from fastapi import FastAPI


def test_load_dotenv_is_called_when_app_main_is_imported():
    with patch("dotenv.load_dotenv") as mock_load:
        import app.main as main_module

        importlib.reload(main_module)

    mock_load.assert_called()


@pytest.mark.anyio
async def test_should_validate_model_settings_before_startup_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    monkeypatch.setenv("OLLAMA_CONTEXT_TOKENS", "1024")
    monkeypatch.setenv("OLLAMA_RESPONSE_RESERVE_TOKENS", "1024")
    memory_policy = patch.object(main, "resolved_memory_policy")

    with memory_policy as resolve_policy:
        with pytest.raises(ValueError) as exc_info:
            async with main.lifespan(FastAPI()):
                pytest.fail("invalid settings must prevent startup")

    message = str(exc_info.value)
    assert "OLLAMA_CONTEXT_TOKENS" in message
    assert "OLLAMA_RESPONSE_RESERVE_TOKENS" in message
    resolve_policy.assert_not_called()
