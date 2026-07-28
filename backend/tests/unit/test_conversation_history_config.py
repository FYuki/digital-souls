from datetime import timedelta

import pytest

from app.conversation_history.config import resolve_conversation_history_config


class TestConversationHistoryConfig:
    def test_should_use_documented_defaults_when_environment_is_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CONVERSATION_TURN_STALE_AFTER_SECONDS", raising=False)
        monkeypatch.delenv("CONVERSATION_HISTORY_RETENTION_DAYS", raising=False)

        config = resolve_conversation_history_config()

        assert config.stale_after == timedelta(seconds=300)
        assert config.retention == timedelta(days=365)

    def test_should_resolve_configured_durations(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CONVERSATION_TURN_STALE_AFTER_SECONDS", "901")
        monkeypatch.setenv("CONVERSATION_HISTORY_RETENTION_DAYS", "30")

        config = resolve_conversation_history_config()

        assert config.stale_after == timedelta(seconds=901)
        assert config.retention == timedelta(days=30)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("CONVERSATION_TURN_STALE_AFTER_SECONDS", "0"),
            ("CONVERSATION_TURN_STALE_AFTER_SECONDS", "-1"),
            ("CONVERSATION_TURN_STALE_AFTER_SECONDS", "1.5"),
            ("CONVERSATION_TURN_STALE_AFTER_SECONDS", "invalid"),
            ("CONVERSATION_HISTORY_RETENTION_DAYS", "0"),
            ("CONVERSATION_HISTORY_RETENTION_DAYS", "-1"),
            ("CONVERSATION_HISTORY_RETENTION_DAYS", "1.5"),
            ("CONVERSATION_HISTORY_RETENTION_DAYS", "invalid"),
        ],
    )
    def test_should_reject_non_positive_or_non_integer_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        key: str,
        value: str,
    ) -> None:
        monkeypatch.delenv("CONVERSATION_TURN_STALE_AFTER_SECONDS", raising=False)
        monkeypatch.delenv("CONVERSATION_HISTORY_RETENTION_DAYS", raising=False)
        monkeypatch.setenv(key, value)

        with pytest.raises(ValueError, match=key):
            resolve_conversation_history_config()
