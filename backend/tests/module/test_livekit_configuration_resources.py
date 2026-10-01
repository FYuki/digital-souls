"""LiveKit設定の失敗時に外部接続資源を生成しないことを検証する。"""
from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from app.livekit_transport.production import (
    LiveKitConfigurationError,
    configure_production_resources,
)


@pytest.mark.parametrize("delay", ["", "invalid", "1.5", "-1"])
def test_invalid_delay_does_not_create_livekit_client(
    monkeypatch: pytest.MonkeyPatch, delay: str,
) -> None:
    monkeypatch.setenv("LIVEKIT_URL", "ws://127.0.0.1:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-secret")
    monkeypatch.setenv("DS_VOICE_DOWNLINK_DELAY_MS", delay)
    sdk = Mock()
    monkeypatch.setattr(
        "app.livekit_transport.production_sdk.livekit_api_module", lambda: sdk,
    )

    with pytest.raises(LiveKitConfigurationError, match="non-negative integer"):
        asyncio.run(configure_production_resources(
            core_session_factory=Mock(),
            screen_session_revoker=Mock(),
            session_trace_recorder=None,
            measurement_kind="automated_test",
            conversations=Mock(),
        ))

    # 設定失敗後に所有者のないHTTP sessionを残さない。
    sdk.LiveKitAPI.assert_not_called()


def test_disabled_livekit_does_not_validate_unused_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DS_VOICE_DOWNLINK_DELAY_MS", "invalid")
    sdk = Mock()
    monkeypatch.setattr(
        "app.livekit_transport.production_sdk.livekit_api_module", lambda: sdk,
    )

    assert asyncio.run(configure_production_resources(
        core_session_factory=None,
        screen_session_revoker=Mock(),
        session_trace_recorder=None,
        measurement_kind="automated_test",
        conversations=Mock(),
    )) is None
    sdk.LiveKitAPI.assert_not_called()
