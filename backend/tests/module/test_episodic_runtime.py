"""通常lifespan・会話HTTP・実SQLiteと構造化推論HTTP境界を結合する。モデル出力は合成。"""

from datetime import UTC, datetime
import json
import sqlite3
import threading
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.memory.episodic.extraction_contracts import ExtractionBatch
from tests.conversation_history_test_support import CONVERSATION_ID, create_repository
from tests.module.test_memory_formation_chat_entrypoints import _character_card

pytestmark = pytest.mark.usefixtures("existing_chat_conversations", "episodic_model_output")


class Model:
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def post(self, _client, url, *, json: dict, **kwargs):
        if url.endswith("/api/show"):
            payload = {"modelfile": "FROM /models/blobs/sha256-" + "0" * 64,
                       "model_info": {"gemma.context_length": 131072}, "capabilities": ["completion"]}
        elif url.endswith("/api/embed"):
            payload = {"embeddings": [[0.25, 0.5, 0.75] for _ in json["input"]]}
        else:
            properties = json.get("format", {}).get("properties", {})
            if "records" in properties:
                self.entered.set()
                assert self.release.wait(timeout=5)
                request = __import__("json").loads(json["messages"][-1]["content"])
                self.calls.append(request)
                content = self.batch(request)
            elif "candidates" in properties:
                content = {"candidates": []}
            else:
                content = {"classification": "NOT_SENSITIVE", "subject_scope": "SELF",
                           "category": "NONE", "reason_code": "NO_SENSITIVE_CONTENT"}
            payload = {"message": {"content": __import__("json").dumps(content, ensure_ascii=False)},
                       "prompt_eval_count": 1000, "eval_count": 100, "done": True}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    def batch(self, request):
        source = next(f for f in request["fragments"] if f["ownership"] == "primary" and f["role"] == "user")
        quote = dict(source_id=source["source_id"], revision=source["revision"], role="user",
                     quote=source["text"], start=source["start"])
        return ExtractionBatch.model_validate({
            "complete": True,
            "records": [
                dict(key="episode", kind="EPISODE", operation="NEW", anchor=quote, sources=[quote],
                     five_w={"what": {"predicate": "昼食の話を聞いた"}, "context": "REPORTED"}),
                dict(key="fact", kind="FACT", operation="NEW", anchor=quote, sources=[quote],
                     five_w={"what": {"predicate": "食べた", "object": "うどん"}, "context": "REPORTED"}),
            ],
            "links": [dict(episode="episode", fact="fact", sources=[quote])],
        }).model_dump(mode="json")


@pytest.fixture
def model(monkeypatch):
    fixture = Model()
    monkeypatch.setenv("INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_INPUT_TOKENS", "32768")
    monkeypatch.setenv("INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS", "4096")
    monkeypatch.setattr(httpx.Client, "post", lambda client, url, **kwargs: fixture.post(client, url, **kwargs))
    monkeypatch.setattr(main, "load_character_card", lambda _: _character_card())
    monkeypatch.setattr("app.llm.router.generate_response", lambda *args, **kwargs: "昼食のお話を聞きました")
    return fixture


def records(paths):
    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        return connection.execute("SELECT id,kind,content_version FROM episodic_records ORDER BY kind").fetchall()


def wait_for_records(paths, count=2):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len(records(paths)) == count:
            return
        time.sleep(0.02)
    raise AssertionError("episodic records were not formed")


def wait_for_receipt(paths):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with sqlite3.connect(paths.sqlite_path) as connection:
            row = connection.execute("SELECT revision,completed_revision FROM memory_thread_jobs").fetchone()
        if row is not None and row[0] == row[1]:
            return
        time.sleep(0.02)
    raise AssertionError("durable extraction did not finish")


@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_reply_does_not_wait_and_lifespan_forms_episode_fact_link(model, runtime_paths, transport):
    model.release.clear()
    with TestClient(main.app) as client:
        try:
            if transport == "http":
                response = client.post("/chat", json={
                    "character": "miori", "conversation_id": str(CONVERSATION_ID), "message": "今日うどんを食べた",
                })
                assert response.status_code == 200
            else:
                with client.websocket_connect(f"/ws/miori?conversation_id={CONVERSATION_ID}") as websocket:
                    websocket.send_json({"type": "text", "message": "今日うどんを食べた"})
                    assert websocket.receive_json()["type"] == "text"
            assert model.entered.wait(timeout=2)
            assert not model.release.is_set()
            assert records(runtime_paths) == []
        finally:
            model.release.set()
        wait_for_records(runtime_paths)
        wait_for_receipt(runtime_paths)
        with sqlite3.connect(runtime_paths.persona_memory_sqlite_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM episodic_links WHERE valid=1").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM approved_memories").fetchone()[0] == 0
            stamp = json.loads(connection.execute("SELECT stamp FROM episodic_versions LIMIT 1").fetchone()[0])
        assert stamp["extraction"]["prompt_version"] == "episode-fact-extraction-v3"
        assert model.calls[0]["entity_labels"]["character:miori"] == "光織"


def test_startup_recovers_persisted_reservation_without_submit_and_restart_does_not_duplicate(
    model, runtime_paths,
):
    from app.conversation_history.models import ProcessingTurnInput
    history = create_repository(runtime_paths.sqlite_path, now=datetime.now(UTC), uuid_factory=uuid4)
    turn = history.create_processing_turn("miori", CONVERSATION_ID, ProcessingTurnInput("今日うどんを食べた"))
    history.complete_turn("miori", CONVERSATION_ID, turn.turn_id, sanitized_assistant_content="昼食のお話ですね")
    with TestClient(main.app):
        wait_for_records(runtime_paths)
        wait_for_receipt(runtime_paths)
    before = records(runtime_paths)
    with TestClient(main.app):
        wait_for_receipt(runtime_paths)
    assert records(runtime_paths) == before
    assert len(model.calls) == 1
