import json

import pytest
from fastapi.testclient import TestClient

from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError

from . import test_postgres_stores
from .conversation_support import turn
from .postgres_history_support import assert_text_absent
from .privacy_support import BINDING
from .support import CALL, TOOL, chunk, completion
from .test_postgres_history_privacy import policy_setup
from .test_postgres_stores import Stores

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


@pytest.mark.parametrize("number", ["4111 1111 1111 1111", "090 1234 5678"])
@pytest.mark.parametrize("local", [False, True])
def test_http_numeric_input_blocks_provider_and_database(
    stores: Stores, number: str, local: bool
) -> None:
    service, provider, classifier, policy = policy_setup(stores, local=local)
    cid = service.create("synthetic").conversation_id
    http = TestClient(
        create_app(service.inference, history_store=service.store, history_policy=policy),
        base_url="http://127.0.0.1",
    )
    body = turn(messages=[{"role": "user", "content": number}]).model_dump()
    response = http.post(f"/v1/characters/synthetic/conversations/{cid}/completions", json=body)
    assert response.status_code == 403
    assert provider.calls == [] and classifier.calls == []
    assert service.read("synthetic", cid).messages == ()
    assert service.store.receipt(BINDING, cid, "r1") is None
    assert_text_absent(stores, number)


@pytest.mark.parametrize("number", ["4111 1111 1111 1111", "090 1234 5678"])
@pytest.mark.parametrize("where", ["assistant", "stream", "tool_args", "tool_result", "schema"])
async def test_numeric_history_boundaries(stores: Stores, number: str, where: str) -> None:
    service, provider, classifier, _ = policy_setup(stores, local=False)
    cid = service.create("synthetic").conversation_id
    body = turn()
    if where == "assistant":
        provider.response["choices"][0]["message"]["content"] = number
    elif where == "stream":
        provider.chunks = [chunk({"content": part}) for part in number] + [chunk({}, "stop")]
        body = turn(stream=True)
    elif where == "tool_args":
        provider.response = completion(tool=True)
        provider.response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            json.dumps({"value": number})
        )
        body = turn(tools=[TOOL])
    elif where == "tool_result":
        provider.response = completion(tool=True)
        await service.complete("synthetic", cid, turn(tools=[TOOL]))
        provider.calls.clear()
        classifier.calls.clear()
        body = turn(
            request_id="r2",
            expected_revision=1,
            tools=[TOOL],
            messages=[
                {
                    "role": "tool",
                    "tool_call_id": CALL["id"],
                    "content": json.dumps({"value": number}),
                }
            ],
        )
    else:
        tool = json.loads(json.dumps(TOOL))
        tool["function"]["description"] = number
        body = turn(tools=[tool])
    before = service.read("synthetic", cid)
    with pytest.raises(CoreError):
        await service.complete("synthetic", cid, body)
    assert service.read("synthetic", cid) == before
    assert service.store.receipt(BINDING, cid, body.request_id) is None
    assert all(number not in json.dumps(payload) for _, payload in classifier.calls)
    if where in {"tool_result", "schema"}:
        assert provider.calls == []
    assert_text_absent(stores, number)


@pytest.mark.parametrize(
    "value",
    [
        r'{"\u0070assword":"synthetic-value","\u0070assword":""}',
        "[" * 1500 + '"password: synthetic"' + "]" * 1500,
    ],
)
async def test_encoded_json_rejected_before_classifier_and_provider(
    stores: Stores, value: str
) -> None:
    service, provider, classifier, _ = policy_setup(stores, local=False)
    cid = service.create("synthetic").conversation_id
    with pytest.raises(CoreError):
        await service.complete(
            "synthetic", cid, turn(messages=[{"role": "user", "content": value}])
        )
    assert provider.calls == [] and classifier.calls == []
    assert service.read("synthetic", cid).messages == ()
    assert service.store.receipt(BINDING, cid, "r1") is None


@pytest.mark.parametrize("number", [4111111111111111, 4111111111111111.0, 4.111111111111111e15])
async def test_numeric_tool_enum_rejected_before_send(stores: Stores, number: int | float) -> None:
    service, provider, classifier, _ = policy_setup(stores, local=False)
    cid = service.create("synthetic").conversation_id
    tool = json.loads(json.dumps(TOOL))
    tool["function"]["parameters"]["properties"]["city"]["enum"] = [number]
    with pytest.raises(CoreError):
        await service.complete("synthetic", cid, turn(tools=[tool]))
    assert provider.calls == [] and classifier.calls == []
    assert service.read("synthetic", cid).messages == ()


@pytest.mark.parametrize("token", ["4.111111111111111e15", "4111111111111111.0"])
async def test_decimal_encoded_value_blocked_before_send(stores: Stores, token: str) -> None:
    service, provider, classifier, _ = policy_setup(stores, local=False)
    cid = service.create("synthetic").conversation_id
    with pytest.raises(CoreError):
        await service.complete(
            "synthetic",
            cid,
            turn(
                messages=[
                    {"role": "user", "content": '{"enum":[' + token + "]}"},
                ]
            ),
        )
    assert provider.calls == [] and classifier.calls == []
    assert service.read("synthetic", cid).messages == ()
