from unittest.mock import MagicMock

from fastapi.testclient import TestClient


def test_should_retry_pending_wal_cleanup_during_application_startup(
    monkeypatch,
) -> None:
    import app.main as main

    retry_pending = MagicMock()
    monkeypatch.setattr(
        main.ConversationWalCleanup,
        "retry_pending",
        retry_pending,
    )
    monkeypatch.setenv("RAG_ENABLED", "false")

    with TestClient(main.app):
        pass

    retry_pending.assert_called_once_with()
