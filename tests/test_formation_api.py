"""Explicit drain injection, bounded input and existing local HTTP authority."""

from typing import cast
from unittest.mock import create_autospec

import pytest
from fastapi.testclient import TestClient

from digital_souls_core.api import create_app
from digital_souls_core.application import Inference
from digital_souls_core.formation import DrainResult, FormationConfig
from digital_souls_core.history import HistoryStore
from digital_souls_core.privacy import PrivacyPolicy

from .conversation_support import SyntheticPolicy
from .support import FakeProvider, character

URL = "/v1/memory/formation/drain"


def fake_history() -> HistoryStore:
    return cast(HistoryStore, create_autospec(HistoryStore, instance=True))


class FakeDrain:
    def __init__(self, history: HistoryStore, policy: PrivacyPolicy) -> None:
        self.history = history
        self.policy = policy
        self.calls: list[int | None] = []
        self.config = FormationConfig(max_jobs=2)

    async def drain(self, *, max_jobs: int | None = None) -> DrainResult:
        self.calls.append(max_jobs)
        return DrainResult(succeeded=1)


@pytest.mark.it1
def test_absent_endpoint_and_explicit_injection() -> None:
    policy = PrivacyPolicy()
    inference = Inference((character("synthetic"),), FakeProvider(), privacy=policy)
    history = fake_history()
    with TestClient(create_app(inference), base_url="http://127.0.0.1") as client:
        assert client.post(URL, json={}).status_code == 404
        assert URL not in client.get("/openapi.json").json()["paths"]
    drain = FakeDrain(history, policy)
    with TestClient(
        create_app(inference, history_store=history, history_policy=policy, formation=drain),
        base_url="http://127.0.0.1",
    ) as client:
        result = client.post(URL, json={"max_jobs": 1})
        assert result.status_code == 200
        assert result.json() == {"status": "success", "succeeded": 1, "failed": 0, "excluded": 0}
        assert drain.calls == [1]
        assert client.post(URL, json={}).status_code == 200
        assert drain.calls == [1, None]


@pytest.mark.it1
@pytest.mark.parametrize(
    "body",
    [
        {"max_jobs": 0},
        {"max_jobs": True},
        {"max_jobs": "1"},
        {"max_jobs": 3},
        {"binding": "caller"},
    ],
)
def test_bounded_content_free_request(body: dict[str, object]) -> None:
    policy = PrivacyPolicy()
    history = fake_history()
    drain = FakeDrain(history, policy)
    with TestClient(
        create_app(
            Inference((), FakeProvider(), privacy=policy),
            history_store=history,
            history_policy=policy,
            formation=drain,
        ),
        base_url="http://127.0.0.1",
    ) as client:
        result = client.post(URL, json=body)
        assert result.status_code == 400
        assert result.json()["error"]["code"] in {"invalid_request", "formation_limit"}
    assert drain.calls == []


@pytest.mark.it1
@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Host": "remote.invalid"}, 400),
        ({"Origin": "https://remote.invalid"}, 403),
        ({"Origin": "http://localhost"}, 403),
    ],
)
def test_host_origin_rejected_before_drain(headers: dict[str, str], status: int) -> None:
    policy = PrivacyPolicy()
    history = fake_history()
    drain = FakeDrain(history, policy)
    with TestClient(
        create_app(
            Inference((), FakeProvider(), privacy=policy),
            history_store=history,
            history_policy=policy,
            formation=drain,
        ),
        base_url="http://127.0.0.1",
    ) as client:
        assert client.post(URL, json={}, headers=headers).status_code == status
    assert drain.calls == []


@pytest.mark.ut
@pytest.mark.parametrize("mismatch", ["store", "policy", "inference", "absent"])
def test_injection_requires_shared_store_and_privacy(mismatch: str) -> None:
    policy = PrivacyPolicy()
    history = fake_history()
    drain = FakeDrain(history, policy)
    with pytest.raises(ValueError, match="formation"):
        create_app(
            Inference(
                (), FakeProvider(), privacy=(PrivacyPolicy() if mismatch == "inference" else policy)
            ),
            history_store=(
                None if mismatch == "absent" else fake_history() if mismatch == "store" else history
            ),
            history_policy=SyntheticPolicy() if mismatch == "policy" else policy,
            formation=drain,
        )
