from typing import Any

import pytest
from pydantic import ValidationError

from digital_souls_core.contracts import (
    AliasCompletion,
    CharacterCompletion,
    CompletionInput,
    Message,
)
from digital_souls_core.conversations import Conversations, request_fingerprint
from digital_souls_core.history import TurnInput

from .content_parts_support import INVALID_CONTENT, parts
from .support import CALL, character

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "model", [CompletionInput, AliasCompletion, CharacterCompletion, TurnInput]
)
@pytest.mark.parametrize("texts", [("Synthetic 日本語",), ("one", "two"), ("", "two\nthree", "")])
def test_input_normalizes_all_roles(
    model: type[CompletionInput] | type[TurnInput], texts: tuple[str, ...]
) -> None:
    raw: list[dict[str, Any]] = [
        {"role": "system", "content": parts(*texts)},
        {"role": "user", "content": parts(*texts)},
        {"role": "assistant", "content": parts(*texts), "tool_calls": [CALL]},
        {"role": "tool", "content": parts(*texts), "tool_call_id": CALL["id"]},
    ]
    selectors: dict[type[CompletionInput] | type[TurnInput], dict[str, Any]] = {
        AliasCompletion: {"model": "synthetic-alias"},
        CharacterCompletion: {"character_id": "synthetic"},
        TurnInput: {"request_id": "r1", "expected_revision": 0},
    }
    body = model.model_validate({"messages": raw, **selectors.get(model, {})})
    assert [m.content for m in body.messages] == ["\n".join(texts)] * 4
    assert all(m["content"] == "\n".join(texts) for m in body.model_dump()["messages"])
    assert raw[0]["content"] == parts(*texts)  # Caller dictionaries remain intact.


@pytest.mark.parametrize("model", [CompletionInput, TurnInput])
def test_input_preserves_null_assistant_tool_call(
    model: type[CompletionInput] | type[TurnInput],
) -> None:
    selectors = {"request_id": "r1", "expected_revision": 0} if model is TurnInput else {}
    body = model.model_validate(
        {
            **selectors,
            "messages": [
                {"role": "assistant", "content": None, "tool_calls": [CALL]},
                {"role": "tool", "content": parts("result"), "tool_call_id": CALL["id"]},
            ],
        }
    )
    assert body.messages[0].content is None
    assert body.messages[0].model_dump()["content"] is None
    assert body.messages[1].content == "result"


@pytest.mark.parametrize("model", [CompletionInput, TurnInput])
@pytest.mark.parametrize("content", INVALID_CONTENT)
def test_input_rejects_invalid_content(
    model: type[CompletionInput] | type[TurnInput], content: Any
) -> None:
    selectors = {"request_id": "r1", "expected_revision": 0} if model is TurnInput else {}
    with pytest.raises(ValidationError):
        model.model_validate({**selectors, "messages": [{"role": "user", "content": content}]})


def test_fingerprint_uses_normalized_text() -> None:
    def body(content: Any) -> TurnInput:
        return TurnInput.model_validate(
            {
                "request_id": "r1",
                "expected_revision": 0,
                "messages": [{"role": "user", "content": content}],
            }
        )

    config = character("synthetic").config
    expected = request_fingerprint(body("one\ntwo"), config)
    assert request_fingerprint(body(parts("one", "two")), config) == expected
    assert request_fingerprint(body(parts("one\ntwo")), config) == expected
    assert request_fingerprint(body(parts("onetwo")), config) != expected


def test_shared_message_and_provider_result_still_reject_arrays() -> None:
    raw = {"role": "assistant", "content": parts("synthetic")}
    with pytest.raises(ValidationError):
        Message.model_validate(raw)
    with pytest.raises(ValidationError):
        Conversations._visible(raw)


def test_input_schema_describes_text_arrays_but_shared_message_stays_text_only() -> None:
    for model in (CompletionInput, TurnInput):
        schema = model.model_json_schema()
        message = schema["properties"]["messages"]["items"]
        while "$ref" in message:
            message = schema["$defs"][message["$ref"].rsplit("/", 1)[-1]]
        content = message["properties"]["content"]
        array = next(option for option in content["anyOf"] if option.get("type") == "array")
        assert array["minItems"] == 1
        part_name = array["items"]["$ref"].rsplit("/", 1)[-1]
        part = schema["$defs"][part_name]
        assert part["additionalProperties"] is False
        assert part["required"] == ["type", "text"]
        assert part["properties"]["type"]["const"] == "text"
        assert part["properties"]["text"]["type"] == "string"
    content = Message.model_json_schema()["properties"]["content"]
    assert all(option.get("type") != "array" for option in content["anyOf"])
