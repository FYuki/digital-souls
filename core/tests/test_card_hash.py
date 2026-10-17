"""Card identity tracks exact startup bytes, independently of parsed prompt text."""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from digital_souls_core.api import create_app
from digital_souls_core.application import Inference
from digital_souls_core.character import load_characters

from .support import ROOT, FakeProvider, character


@pytest.mark.ut
@pytest.mark.parametrize("change", ["whitespace", "prompt"])
def test_card_hash_uses_exact_bytes_and_is_pinned(tmp_path: Path, change: str) -> None:
    config = json.loads((ROOT / "examples/characters.json").read_text())
    config[0]["card_path"] = "card.json"
    path = tmp_path / "characters.json"
    path.write_text(json.dumps(config))
    card_path = tmp_path / "card.json"
    original = (ROOT / "characters/miori/miori.card.json").read_bytes()
    # CRLF must not be normalized by text-mode reading.
    original = original.replace(b"\n", b"\r\n")
    card_path.write_bytes(original)
    first = load_characters(path)[0]
    assert first.card_sha256 == hashlib.sha256(original).hexdigest()
    if change == "whitespace":
        changed = original + b"\r\n"
    else:
        card_data = json.loads(original)
        card_data["data"]["description"] = "Synthetic changed persona"
        changed = json.dumps(card_data, ensure_ascii=False).encode("utf-8")
    card_path.write_bytes(changed)
    second = load_characters(path)[0]
    assert (second.card == first.card) is (change == "whitespace")
    assert second.card_sha256 == hashlib.sha256(changed).hexdigest()
    assert second.card_sha256 != first.card_sha256
    assert first.card_sha256 == hashlib.sha256(original).hexdigest()


@pytest.mark.ut
@pytest.mark.parametrize("invalid", [b'{"SYNTHETIC_PRIVATE_CARD":', b"\xffSYNTHETIC_PRIVATE_CARD"])
def test_invalid_card_error_does_not_contain_bytes(tmp_path: Path, invalid: bytes) -> None:
    config = json.loads((ROOT / "examples/characters.json").read_text())
    config[0]["card_path"] = "card.json"
    path = tmp_path / "characters.json"
    path.write_text(json.dumps(config))
    (tmp_path / "card.json").write_bytes(invalid)
    with pytest.raises(ValueError) as error:
        load_characters(path)
    assert "SYNTHETIC_PRIVATE_CARD" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.it1
@pytest.mark.parametrize("endpoint", ["chat", "character"])
@pytest.mark.parametrize("stream", [False, True])
def test_stateless_card_hash_header(endpoint: str, stream: bool) -> None:
    char = character()
    fake = FakeProvider()
    http = TestClient(create_app(Inference((char,), fake)), base_url="http://127.0.0.1")
    selector = (
        {"model": char.config.alias}
        if endpoint == "chat"
        else {"character_id": char.config.character_id}
    )
    response = http.post(
        f"/v1/{endpoint}/completions",
        json={
            **selector,
            "messages": [{"role": "user", "content": "Synthetic hello"}],
            "stream": stream,
        },
    )
    assert response.status_code == 200
    expected = hashlib.sha256((ROOT / "characters/miori/miori.card.json").read_bytes()).hexdigest()
    assert response.headers["X-Character-Card-SHA256"] == expected
    assert http.get(f"/v1/characters/{char.config.character_id}").json()["card_sha256"] == expected


@pytest.mark.it1
@pytest.mark.parametrize("stream", [False, True])
def test_history_header_uses_saved_receipt_including_retry(stream: bool) -> None:
    from unittest.mock import Mock

    from digital_souls_core.contracts import Message
    from digital_souls_core.conversations import request_fingerprint
    from digital_souls_core.history import HistoryStore, Receipt, Snapshot

    from .conversation_support import SyntheticPolicy, turn

    char = character()
    fake = FakeProvider()
    store = Mock(spec=HistoryStore)
    store.read.return_value = Snapshot("synthetic", 0, ())
    store.receipt.return_value = None
    saved_hash = "a" * 64
    body = turn(stream=stream)
    receipt = Receipt(
        request_fingerprint(body, char.config),
        1,
        Message(role="assistant", content="Synthetic winner"),
        "stop",
        card_sha256=saved_hash,
    )
    store.append.return_value = receipt
    http = TestClient(
        create_app(Inference((char,), fake), history_store=store, history_policy=SyntheticPolicy()),
        base_url="http://127.0.0.1",
    )
    url = "/v1/characters/miori/conversations/synthetic/completions"
    first = http.post(url, json=body.model_dump())
    assert first.status_code == 200
    assert first.headers["X-Character-Card-SHA256"] == saved_hash
    assert store.append.call_args.kwargs["card_sha256"] == char.card_sha256
    store.receipt.return_value = receipt
    retry = http.post(url, json=body.model_dump())
    assert retry.status_code == 200
    assert retry.headers["X-Character-Card-SHA256"] == saved_hash
    assert retry.text == first.text and len(fake.calls) == 1
