"""本番lifespanのLLM未構成・接続停止時の縮退と、誤設定の拒否を検証する。"""
import os
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main
from app.inference.errors import InferenceError, InferenceErrorCategory
from app.inference.runtime import InferenceRuntime


@pytest.fixture
def notification_config(tmp_path, monkeypatch):
    config = tmp_path / "notifications.json"
    config.write_text(json.dumps({"version": 1, "registrations": []}))
    monkeypatch.setenv("DS_NOTIFICATION_CONFIG", str(config))
    monkeypatch.delenv("DS_MCP_CONFIG", raising=False)
    monkeypatch.delenv("DS_MCP_EVENT_CONFIG", raising=False)
    return config


def clear_inference(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("INFERENCE_TARGET_"):
            monkeypatch.delenv(name)


def test_unconfigured_llm_starts_actual_app_without_conversation_storage(
    notification_config, monkeypatch, runtime_paths,
):
    clear_inference(monkeypatch)
    monkeypatch.setattr(main, "create_inference_runtime", lambda *_: pytest.fail("LLM must not be initialized"))
    with TestClient(main.app) as client:
        assert client.get("/health/ready").json() == {"status": "ready", "mode": "notifications_only"}
        assert client.get("/health/inference").status_code == 503
        response = client.get("/notifications")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["notification_only"] is True
        assert client.get("/addon-admin/connections").status_code == 200
        assert client.get("/characters").status_code == 200
        for path in ("/chat", "/ui-settings", "/characters/miori/conversations"):
            denied = client.post(path, json={}) if path == "/chat" else client.get(path)
            assert denied.status_code == 503
            assert denied.json()["detail"]["code"] == "conversation_unavailable"
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws"):
                pytest.fail("voice must not start")
        assert error.value.code == 1013
        assert not runtime_paths.sqlite_path.exists()
        assert not runtime_paths.persona_memory_sqlite_path.exists()
    assert main.app.state.notification_only is False
    assert not hasattr(main.app.state, "notifications")


@pytest.mark.parametrize("category", [InferenceErrorCategory.UNAVAILABLE, InferenceErrorCategory.TIMEOUT])
def test_required_llm_connection_failure_closes_inference_and_starts_notifications(
    notification_config, monkeypatch, category,
):
    closed = []
    original_close = InferenceRuntime.close
    def probe(_):
        raise InferenceError(category, retryable=True)
    def close(runtime):
        closed.append(runtime)
        original_close(runtime)
    monkeypatch.setattr(InferenceRuntime, "probe_startup", probe)
    monkeypatch.setattr(InferenceRuntime, "close", close)
    with TestClient(main.app) as client:
        assert len(closed) == 1
        assert client.get("/notifications").status_code == 200
        assert client.get("/health/ready").json()["mode"] == "notifications_only"


@pytest.mark.parametrize("category", [InferenceErrorCategory.AUTHENTICATION_FAILED, InferenceErrorCategory.MODEL_NOT_FOUND])
def test_invalid_provider_configuration_does_not_fall_back(notification_config, monkeypatch, category):
    def probe(_):
        raise InferenceError(category, retryable=False)
    monkeypatch.setattr(InferenceRuntime, "probe_startup", probe)
    with pytest.raises(InferenceError):
        with TestClient(main.app):
            pytest.fail("bad inference configuration must prevent startup")


def test_partial_inference_configuration_does_not_fall_back(notification_config, monkeypatch):
    clear_inference(monkeypatch)
    monkeypatch.setenv("INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS", "100")
    with pytest.raises(ValueError):
        with TestClient(main.app):
            pytest.fail("partial configuration must not be hidden")


def test_without_notification_configuration_retains_startup_failure(monkeypatch):
    clear_inference(monkeypatch)
    monkeypatch.delenv("DS_NOTIFICATION_CONFIG", raising=False)
    with pytest.raises(ValueError):
        with TestClient(main.app):
            pytest.fail("normal startup still requires inference")


def test_notification_only_still_blocks_incomplete_restore(notification_config, monkeypatch, runtime_paths):
    clear_inference(monkeypatch)
    runtime_paths.restore_intent_path.write_text("{}")
    with pytest.raises(Exception, match="restore"):
        with TestClient(main.app):
            pytest.fail("an incomplete restore must prevent startup")
    assert not hasattr(main.app.state, "notifications")
