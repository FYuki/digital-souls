from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


def _patch_privacy_startup(monkeypatch: pytest.MonkeyPatch):
    import app.main as main
    from app.model_settings import resolve_model_settings

    resolved_policy = MagicMock(name="resolved_policy")
    resolved_policy.privacy = MagicMock(name="privacy_policy")
    scanner = MagicMock(name="privacy_scanner")
    sanitizer = MagicMock(name="history_sanitizer")
    history_service = MagicMock(name="conversation_history_service")
    resolve_policy = MagicMock(return_value=resolved_policy)
    create_scanner = MagicMock(return_value=scanner)
    create_sanitizer = MagicMock(return_value=sanitizer)
    resolve_chat = MagicMock(
        return_value=main._chat_runtime.ChatRuntimeConfig(
            rag_enabled=False,
            memory_policy=None,
            privacy_scanner=None,
            prompt_config=resolve_model_settings({}),
        )
    )
    monkeypatch.setattr(main, "resolved_memory_policy", resolve_policy)
    monkeypatch.setattr(main, "create_privacy_scanner", create_scanner)
    monkeypatch.setattr(main, "create_history_sanitizer", create_sanitizer)
    create_history_service = MagicMock(return_value=history_service)
    monkeypatch.setattr(
        main,
        "ConversationHistoryService",
        create_history_service,
    )
    monkeypatch.setattr(main._chat_runtime, "resolve_chat_runtime_config", resolve_chat)
    monkeypatch.setenv("RAG_ENABLED", "false")
    return (
        main,
        resolved_policy,
        scanner,
        sanitizer,
        resolve_policy,
        create_scanner,
        create_sanitizer,
        create_history_service,
        history_service,
        resolve_chat,
    )


def test_should_resolve_policy_once_and_inject_same_instance_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        main,
        policy,
        scanner,
        sanitizer,
        resolve_policy,
        create_scanner,
        create_sanitizer,
        create_history_service,
        history_service,
        resolve_chat,
    ) = _patch_privacy_startup(monkeypatch)

    with TestClient(main.app):
        repository = main.app.state.conversation_history_repository
        assert not hasattr(main.app.state, "privacy_scanner")
        assert not hasattr(main.app.state, "history_sanitizer")

    resolve_policy.assert_called_once_with()
    create_scanner.assert_called_once_with(policy.privacy)
    create_sanitizer.assert_called_once_with(scanner, policy.privacy)
    create_history_service.assert_called_once_with(repository, sanitizer)
    assert history_service is not None
    resolve_chat.assert_called_once()
    assert resolve_chat.call_args.args[:2] == (policy, scanner)
    assert resolve_chat.call_args.args[2].ollama_chat_model == "gemma4:e4b"


def test_should_initialize_privacy_even_when_rag_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        main,
        _policy,
        _scanner,
        _sanitizer,
        resolve_policy,
        create_scanner,
        create_sanitizer,
        _create_history_service,
        _history_service,
        _resolve_chat,
    ) = _patch_privacy_startup(monkeypatch)

    with TestClient(main.app):
        pass

    resolve_policy.assert_called_once_with()
    create_scanner.assert_called_once()
    create_sanitizer.assert_called_once()


def test_should_not_publish_privacy_infrastructure_on_app_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main, *_rest = _patch_privacy_startup(monkeypatch)

    with TestClient(main.app):
        assert not hasattr(main.app.state, "privacy_scanner")
        assert not hasattr(main.app.state, "history_sanitizer")

    assert not hasattr(main.app.state, "privacy_scanner")
    assert not hasattr(main.app.state, "history_sanitizer")


def test_should_fail_before_request_handling_when_policy_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main

    resolve_policy = MagicMock(side_effect=ValueError("invalid privacy policy"))
    create_scanner = MagicMock()
    create_sanitizer = MagicMock()
    monkeypatch.setattr(main, "resolved_memory_policy", resolve_policy)
    monkeypatch.setattr(main, "create_privacy_scanner", create_scanner)
    monkeypatch.setattr(main, "create_history_sanitizer", create_sanitizer)

    with pytest.raises(ValueError, match="invalid privacy policy"):
        with TestClient(main.app):
            raise AssertionError("startup must fail before yielding")

    resolve_policy.assert_called_once_with()
    create_scanner.assert_not_called()
    create_sanitizer.assert_not_called()
    assert not hasattr(main.app.state, "privacy_scanner")
    assert not hasattr(main.app.state, "history_sanitizer")
