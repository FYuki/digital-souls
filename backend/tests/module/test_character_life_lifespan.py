"""既存FastAPI lifespanへの実DBOS接続と終了順を検証する。外部推論はmodule fixtureで置換。"""

from fastapi.testclient import TestClient


def test_enabled_main_lifespan_owns_single_gate_and_collects_runtime(
    monkeypatch, runtime_paths
):
    from app import main
    from app.character_life import runtime as life_runtime

    tools = []
    original = main.ToolRuntime

    class RecordingToolRuntime(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            tools.append(self)

    monkeypatch.setattr(main, "ToolRuntime", RecordingToolRuntime)
    monkeypatch.setenv("DS_CHARACTER_LIFE_ENABLED", "true")
    monkeypatch.setenv("DS_CHARACTER_LIFE_CRON", "0 0 1 1 *")
    monkeypatch.setenv("INFERENCE_TARGET_CHARACTER_LIFE", "ollama/gemma4:e4b")
    monkeypatch.setenv("INFERENCE_TARGET_CHARACTER_LIFE_MAX_INPUT_TOKENS", "12000")
    monkeypatch.setenv("INFERENCE_TARGET_CHARACTER_LIFE_MAX_OUTPUT_TOKENS", "1024")
    monkeypatch.delenv("DS_MCP_CONFIG", raising=False)
    with TestClient(main.app) as client:
        runtime = main.app.state.character_life_runtime
        assert runtime.started
        assert runtime.service.gate is tools[0].gate
        assert runtime.system_path.parent == runtime_paths.data_root
        assert "miori" in runtime.characters()
        response = client.get("/character-life/miori")
        assert response.status_code == 200
        assert (
            response.json()["dependencies"]["episode_reflection"]
            == "deferred_issue_100"
        )
        assert client.get("/health/ready").status_code == 200
    assert not runtime.started
    assert not runtime.service.tasks
    assert life_runtime._owner is None
    assert not hasattr(main.app.state, "character_life_runtime")
    assert not runtime.service.gate._loops
