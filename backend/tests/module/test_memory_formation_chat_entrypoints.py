from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.prompting import CharacterPrompt
from tests.conversation_history_test_support import CONVERSATION_ID


pytestmark = pytest.mark.usefixtures("existing_chat_conversations")


class RecordingFormationScheduler:
    def __init__(self) -> None:
        self.jobs: list[object] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    def submit(self, job: object) -> None:
        assert self.started
        self.jobs.append(job)

    def is_busy(self) -> bool:
        return False

    async def stop(self) -> None:
        self.stopped = True


class ConfigRecordingFormationScheduler(RecordingFormationScheduler):
    def __init__(self, *, max_queue_age_seconds: int, queue_maxsize: int) -> None:
        super().__init__()
        self.max_queue_age_seconds = max_queue_age_seconds
        self.queue_maxsize = queue_maxsize


class FailingExtractorRequest:
    def __init__(self, private_values: tuple[str, ...]) -> None:
        self.private_values = private_values
        self.attempted = threading.Event()

    def __call__(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        del json, timeout
        self.attempted.set()
        raise RuntimeError(" ".join(self.private_values))


class RecordingNoOpDomainRouter:
    def __init__(self) -> None:
        self.turns: list[object] = []

    def dispatch(self, turn: object) -> None:
        self.turns.append(turn)


class BlockingExtractorRequest:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        del timeout
        assert isinstance(json.get("format"), dict)
        self.entered.set()
        self.release.wait(timeout=5)
        return httpx.Response(
            200,
            json={"message": {"content": '{"candidates": []}'}},
            request=httpx.Request("POST", url),
        )


class SequencedExtractorRequest:
    def __init__(self, outputs: list[str], *, blocked_call: int | None = None) -> None:
        self._outputs = outputs
        self._blocked_call = blocked_call
        self.calls: list[dict[str, object]] = []
        self.entered = [threading.Event() for _ in outputs]
        self.release = threading.Event()

    def __call__(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        del timeout
        index = len(self.calls)
        self.calls.append(json)
        self.entered[index].set()
        if index == self._blocked_call:
            self.release.wait(timeout=5)
        return httpx.Response(
            200,
            json={"message": {"content": self._outputs[index]}},
            request=httpx.Request("POST", url),
        )


class TimingOutExtractorRequest:
    def __init__(self, expected_calls: int) -> None:
        self.expected_calls = expected_calls
        self.calls = 0
        self.completed = threading.Event()

    def __call__(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        del json, timeout
        self.calls += 1
        if self.calls == self.expected_calls:
            self.completed.set()
        raise httpx.ReadTimeout(
            "synthetic extractor timeout",
            request=httpx.Request("POST", url),
        )


def _is_extractor_request(payload: dict[str, object]) -> bool:
    response_format = payload.get("format")
    if not isinstance(response_format, dict):
        return False
    properties = response_format.get("properties")
    return isinstance(properties, dict) and "candidates" in properties


def _privacy_response(url: str) -> httpx.Response:
    content = json.dumps(
        {
            "classification": "NOT_SENSITIVE",
            "subject_scope": "SELF",
            "category": "NONE",
            "reason_code": "NO_SENSITIVE_CONTENT",
        }
    )
    return httpx.Response(
        200,
        json={"message": {"content": content}},
        request=httpx.Request("POST", url),
    )


def _route_ollama_post(extractor_request):
    def ollama_post(
        _client: httpx.Client,
        url: str,
        *,
        json: dict[str, object],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        if url.endswith("/api/show"):
            return httpx.Response(
                200,
                json={"modelfile": "FROM /models/blobs/sha256-" + "0" * 64},
                request=httpx.Request("POST", url),
            )
        if _is_extractor_request(json):
            return extractor_request(url, json=json, timeout=timeout)
        return _privacy_response(url)

    return ollama_post


def _character_card() -> MagicMock:
    card = MagicMock()
    card.data.character_book = None
    card.to_character_prompt.return_value = CharacterPrompt(
        description="",
        personality="",
        scenario="",
        system_prompt="# synthetic prompt",
        mes_example="",
        post_history_instructions="",
    )
    return card


def test_http_and_websocket_share_the_same_identifier_only_job_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = RecordingFormationScheduler()
    monkeypatch.setattr(
        main,
        "MemoryFormationScheduler",
        lambda *args, **kwargs: scheduler,
        raising=False,
    )

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/chat",
                    json={
                        "character": "miori",
                        "conversation_id": str(CONVERSATION_ID),
                        "message": "http synthetic input",
                    },
                )
                with client.websocket_connect(
                    f"/ws/miori?conversation_id={CONVERSATION_ID}"
                ) as websocket:
                    websocket.send_json(
                        {"type": "text", "message": "websocket synthetic input"}
                    )
                    websocket.receive_json()

    assert response.status_code == 200
    assert scheduler.started
    assert scheduler.stopped
    assert len(scheduler.jobs) == 2
    for job in scheduler.jobs:
        payload = asdict(job)
        assert set(payload) == {"character_id", "conversation_id", "turn_id"}
        assert payload["character_id"] == "miori"
        assert payload["conversation_id"] == CONVERSATION_ID


def test_runtime_passes_configured_queue_age_to_the_job_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedulers: list[ConfigRecordingFormationScheduler] = []

    def scheduler_factory(*args, **kwargs) -> ConfigRecordingFormationScheduler:
        del args
        scheduler = ConfigRecordingFormationScheduler(
            max_queue_age_seconds=kwargs["max_queue_age_seconds"],
            queue_maxsize=kwargs["queue_maxsize"],
        )
        schedulers.append(scheduler)
        return scheduler

    monkeypatch.setenv("MEMORY_FORMATION_MAX_QUEUE_AGE_SECONDS", "45")
    monkeypatch.setenv("MEMORY_FORMATION_QUEUE_MAXSIZE", "25")
    monkeypatch.setattr(
        main,
        "MemoryFormationScheduler",
        scheduler_factory,
        raising=False,
    )

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:
                _send_http_message(client, "queue age synthetic input")

    observed = (
        (
            schedulers[0].max_queue_age_seconds,
            schedulers[0].queue_maxsize,
            len(schedulers[0].jobs),
        )
        if schedulers
        else (None, None, 0)
    )
    assert observed == (45, 25, 1)


@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_chat_response_does_not_wait_for_memory_extraction(
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    extractor_request = BlockingExtractorRequest()
    monkeypatch.setattr(
        httpx.Client,
        "post",
        _route_ollama_post(extractor_request),
    )
    response_received = threading.Event()
    responses: list[object] = []
    errors: list[BaseException] = []

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:

                def send_message() -> None:
                    try:
                        if transport == "http":
                            response = client.request(
                                "POST",
                                "/chat",
                                json={
                                    "character": "miori",
                                    "conversation_id": str(CONVERSATION_ID),
                                    "message": "http non-blocking input",
                                },
                            )
                            responses.append(response.status_code)
                        else:
                            with client.websocket_connect(
                                f"/ws/miori?conversation_id={CONVERSATION_ID}"
                            ) as websocket:
                                websocket.send_json(
                                    {
                                        "type": "text",
                                        "message": "websocket non-blocking input",
                                    }
                                )
                                responses.append(websocket.receive_json())
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        response_received.set()

                request_thread = threading.Thread(target=send_message)
                request_thread.start()
                try:
                    assert extractor_request.entered.wait(timeout=1)
                    assert response_received.wait(timeout=1)
                    assert not extractor_request.release.is_set()
                    assert errors == []
                    if transport == "http":
                        assert responses == [200]
                    else:
                        assert len(responses) == 1
                        assert isinstance(responses[0], dict)
                        assert responses[0]["type"] == "text"
                finally:
                    extractor_request.release.set()
                    request_thread.join(timeout=2)

                assert not request_thread.is_alive()


def _approved_memory_count(database_path) -> int:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM approved_memories").fetchone()
    assert row is not None
    return int(row[0])


def _approved_memory_types(database_path) -> list[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT memory_type FROM approved_memories ORDER BY created_at, id"
        ).fetchall()
    return [str(row[0]) for row in rows]


def _wait_for_approved_memory(database_path, expected: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _approved_memory_count(database_path) == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"approved memory count did not reach {expected}")


def _send_http_message(client: TestClient, message: str) -> None:
    response = client.request(
        "POST",
        "/chat",
        json={
            "character": "miori",
            "conversation_id": str(CONVERSATION_ID),
            "message": message,
        },
    )
    assert response.status_code == 200


def _valid_preference_response() -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "memory_type": "USER_PREFERENCE",
                    "structured_value": {"polarity": "LIKE", "object": "紅茶"},
                    "date_expressions": [],
                }
            ]
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    ("candidate", "expected_memory_type"),
    [
        (
            {
                "memory_type": "EPISODIC_EVENT",
                "structured_value": {
                    "event_type": "ACHIEVEMENT",
                    "subject": "USER",
                    "topic": "資格試験への合格",
                },
            },
            "EPISODIC_EVENT",
        ),
        (
            {
                "memory_type": "USER_PREFERENCE",
                "structured_value": {"polarity": "LIKE", "object": "紅茶"},
            },
            "USER_PREFERENCE",
        ),
        (
            {
                "memory_type": "INTERACTION_PREFERENCE",
                "structured_value": {"aspect": "TONE", "value": "穏やかな口調"},
            },
            "INTERACTION_PREFERENCE",
        ),
    ],
    ids=["episodic-event", "user-preference", "interaction-preference"],
)
def test_http_turn_extracts_each_allowlisted_type_to_automatic_persistence(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths,
    candidate: dict[str, object],
    expected_memory_type: str,
) -> None:
    extractor_response = json.dumps(
        {"candidates": [{**candidate, "date_expressions": []}]},
        ensure_ascii=False,
    )
    privacy_response = json.dumps(
        {
            "classification": "NOT_SENSITIVE",
            "subject_scope": "SELF",
            "category": "NONE",
            "reason_code": "NO_SENSITIVE_CONTENT",
        }
    )

    def ollama_post(
        _client: httpx.Client,
        url: str,
        *,
        json: dict[str, object],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        del timeout
        content = extractor_response if _is_extractor_request(json) else privacy_response
        return httpx.Response(
            200,
            json={"message": {"content": content}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", ollama_post)

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:
                response = client.request(
                    "POST",
                    "/chat",
                    json={
                        "character": "miori",
                        "conversation_id": str(CONVERSATION_ID),
                        "message": "合成された記憶形成入力",
                    },
                )
                assert response.status_code == 200
                _wait_for_approved_memory(
                    runtime_paths.persona_memory_sqlite_path,
                    1,
                )

    assert _approved_memory_types(runtime_paths.persona_memory_sqlite_path) == [
        expected_memory_type
    ]


def test_noop_domain_dispatch_preserves_automatic_persona_admission(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths,
) -> None:
    router = RecordingNoOpDomainRouter()
    worker_class = getattr(main, "MemoryFormationWorker", None)

    def worker_factory(*args, **kwargs):
        del args
        assert worker_class is not None
        return worker_class(**{**kwargs, "domain_router": router})

    monkeypatch.setattr(main, "MemoryFormationWorker", worker_factory, raising=False)
    extractor_response = _valid_preference_response()
    privacy_response = json.dumps(
        {
            "classification": "NOT_SENSITIVE",
            "subject_scope": "SELF",
            "category": "NONE",
            "reason_code": "NO_SENSITIVE_CONTENT",
        }
    )

    def ollama_post(
        _client: httpx.Client,
        url: str,
        *,
        json: dict[str, object],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        del timeout
        content = extractor_response if _is_extractor_request(json) else privacy_response
        return httpx.Response(
            200,
            json={"message": {"content": content}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", ollama_post)

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:
                _send_http_message(client, "domain seam synthetic input")
                _wait_for_approved_memory(runtime_paths.persona_memory_sqlite_path, 1)

    assert len(router.turns) == 1
    assert _approved_memory_types(runtime_paths.persona_memory_sqlite_path) == [
        "USER_PREFERENCE"
    ]


def test_extractor_failure_logs_metadata_without_private_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_values = (
        "synthetic-private-source-entrypoint",
        "synthetic-private-prompt-entrypoint",
        "synthetic-private-candidate-entrypoint",
    )
    extractor_request = FailingExtractorRequest(private_values)
    monkeypatch.setattr(
        httpx.Client,
        "post",
        _route_ollama_post(extractor_request),
    )
    caplog.set_level(logging.DEBUG)

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:
                _send_http_message(client, private_values[0])
                attempted = extractor_request.attempted.wait(timeout=1)

    observed = caplog.text
    assert attempted
    for private_value in private_values:
        assert private_value not in observed


@pytest.mark.parametrize(
    "invalid_output",
    [
        "not-json",
        json.dumps(
            {"candidates": [{"memory_type": "UNKNOWN", "structured_value": {}}]}
        ),
        json.dumps({"candidates": [{"memory_type": "USER_PREFERENCE"}]}),
        json.dumps(
            {
                "candidates": [
                    {
                        "memory_type": "USER_PREFERENCE",
                        "structured_value": {
                            "polarity": "LIKE",
                            "object": "紅茶",
                        },
                        "unexpected": "field",
                    }
                ]
            }
        ),
        json.dumps(
            {
                "candidates": [
                    {
                        "memory_type": "USER_PREFERENCE",
                        "structured_value": {
                            "polarity": "LIKE",
                            "object": f"対象{index}",
                        },
                    }
                    for index in range(4)
                ]
            },
            ensure_ascii=False,
        ),
    ],
    ids=["invalid-json", "unknown-enum", "missing-field", "extra-field", "over-limit"],
)
def test_invalid_extractor_batch_is_not_automatically_persisted(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths,
    invalid_output: str,
) -> None:
    extractor_request = SequencedExtractorRequest(
        [_valid_preference_response(), invalid_output]
    )
    privacy_response = json.dumps(
        {
            "classification": "NOT_SENSITIVE",
            "subject_scope": "SELF",
            "category": "NONE",
            "reason_code": "NO_SENSITIVE_CONTENT",
        }
    )

    def ollama_post(
        _client: httpx.Client,
        url: str,
        *,
        json: dict[str, object],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        if _is_extractor_request(json):
            return extractor_request(url, json=json, timeout=timeout)
        return httpx.Response(
            200,
            json={"message": {"content": privacy_response}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", ollama_post)

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:
                _send_http_message(client, "valid synthetic input")
                _send_http_message(client, "invalid synthetic input")

    assert _approved_memory_types(runtime_paths.persona_memory_sqlite_path) == [
        "USER_PREFERENCE"
    ]


def test_extractor_receives_only_current_user_and_latest_completed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_response = json.dumps({"candidates": []})
    extractor_request = SequencedExtractorRequest([empty_response] * 3)
    monkeypatch.setattr(
        httpx.Client,
        "post",
        _route_ollama_post(extractor_request),
    )

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch(
            "app.llm.router.generate_response",
            side_effect=["reply-one", "reply-two", "reply-three"],
        ):
            with TestClient(main.app) as client:
                for index, message in enumerate(
                    ["oldest-user", "latest-user", "current-user"]
                ):
                    _send_http_message(client, message)

    transferred = (
        json.dumps(extractor_request.calls[2]["messages"], ensure_ascii=False)
        if len(extractor_request.calls) == 3
        else ""
    )
    assert (
        "current-user" in transferred
        and "latest-user" in transferred
        and "reply-two" in transferred
        and "oldest-user" not in transferred
        and "reply-one" not in transferred
        and "reply-three" not in transferred
    )


def test_runtime_passes_schema_zero_temperature_and_configured_output_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extractor_request = SequencedExtractorRequest([json.dumps({"candidates": []})])
    monkeypatch.setenv("MEMORY_FORMATION_MAX_OUTPUT_TOKENS", "321")
    monkeypatch.setattr(
        httpx.Client,
        "post",
        _route_ollama_post(extractor_request),
    )

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:
                _send_http_message(client, "schema synthetic input")

    assert len(extractor_request.calls) == 1 and isinstance(
        extractor_request.calls[0]["format"], dict
    )
    assert extractor_request.calls[0]["options"] == {
        "temperature": 0,
        "num_predict": 321,
    }


def test_runtime_rejects_invalid_formation_setting_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORY_FORMATION_MAX_ATTEMPTS", "")

    with pytest.raises(ValueError, match="MEMORY_FORMATION_MAX_ATTEMPTS"):
        with TestClient(main.app):
            pass


def test_timeout_retries_to_configured_limit_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths,
) -> None:
    extractor_request = TimingOutExtractorRequest(expected_calls=2)
    monkeypatch.setenv("MEMORY_FORMATION_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(
        httpx.Client,
        "post",
        _route_ollama_post(extractor_request),
    )

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:
                _send_http_message(client, "timeout synthetic input")
                retries_completed = extractor_request.completed.wait(timeout=1)

    assert (
        retries_completed,
        extractor_request.calls,
        _approved_memory_count(runtime_paths.persona_memory_sqlite_path),
    ) == (True, 2, 0)


def test_single_worker_processes_queued_jobs_serially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_response = json.dumps({"candidates": []})
    extractor_request = SequencedExtractorRequest(
        [empty_response, empty_response], blocked_call=0
    )
    monkeypatch.setattr(
        httpx.Client,
        "post",
        _route_ollama_post(extractor_request),
    )

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:
                _send_http_message(client, "first queued input")
                first_started = extractor_request.entered[0].wait(timeout=1)
                _send_http_message(client, "second queued input")
                second_started_while_first_blocked = extractor_request.entered[1].wait(
                    timeout=0.1
                )
                extractor_request.release.set()
                second_started_after_release = extractor_request.entered[1].wait(
                    timeout=1
                )

    assert (
        first_started,
        second_started_while_first_blocked,
        second_started_after_release,
        len(extractor_request.calls),
    ) == (True, False, True, 2)


def test_worker_rereads_queued_turn_and_skips_deleted_source(
    monkeypatch: pytest.MonkeyPatch,
    conversation_history_database_path,
) -> None:
    empty_response = json.dumps({"candidates": []})
    extractor_request = SequencedExtractorRequest(
        [empty_response, empty_response], blocked_call=0
    )
    monkeypatch.setattr(
        httpx.Client,
        "post",
        _route_ollama_post(extractor_request),
    )

    with patch("app.main.load_character_card", return_value=_character_card()):
        with patch("app.llm.router.generate_response", return_value="synthetic reply"):
            with TestClient(main.app) as client:
                _send_http_message(client, "blocking source")
                extractor_request.entered[0].wait(timeout=1)
                _send_http_message(client, "deleted queued source")
                with sqlite3.connect(conversation_history_database_path) as connection:
                    deleted = connection.execute(
                        "DELETE FROM conversation_turns WHERE user_content = ?",
                        ("deleted queued source",),
                    ).rowcount
                assert deleted == 1
                extractor_request.release.set()

    assert len(extractor_request.calls) == 1
