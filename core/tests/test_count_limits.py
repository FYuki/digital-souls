"""Count limits are removed while input shape and exclusion validation remain."""

from typing import Any

import pytest
from pydantic import ValidationError

from digital_souls_core.contracts import CompletionInput
from digital_souls_core.history import TurnInput

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "model,field,count",
    [
        (CompletionInput, "messages", 257),
        (CompletionInput, "tools", 129),
        (TurnInput, "messages", 129),
        (TurnInput, "tools", 129),
        (TurnInput, "memory_excluded_indices", 129),
    ],
)
def test_input_accepts_counts_above_previous_limits(
    model: type[CompletionInput] | type[TurnInput], field: str, count: int
) -> None:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": "Synthetic"}]}
    if model is TurnInput:
        body.update(request_id="synthetic", expected_revision=0)
    if field == "tools":
        body[field] = [
            {"type": "function", "function": {"name": f"synthetic_{i}", "parameters": {}}}
            for i in range(count)
        ]
    else:
        body["messages"] *= count
        if field == "memory_excluded_indices":
            body[field] = list(range(count))
    validated = model.model_validate(body)
    assert len(getattr(validated, field)) == count


@pytest.mark.parametrize("model", [CompletionInput, TurnInput])
@pytest.mark.parametrize("field", ["messages", "tools"])
def test_empty_input_lists_remain_invalid(
    model: type[CompletionInput] | type[TurnInput], field: str
) -> None:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": "Synthetic"}], field: []}
    if model is TurnInput:
        body.update(request_id="synthetic", expected_revision=0)
    with pytest.raises(ValidationError):
        model.model_validate(body)


@pytest.mark.parametrize("indices", [[0, 0], [129], [-1], [True], [0.0]])
def test_large_turn_still_validates_exclusion_indices(indices: list[Any]) -> None:
    with pytest.raises(ValidationError) as error:
        TurnInput.model_validate(
            {
                "request_id": "synthetic",
                "expected_revision": 0,
                "messages": [{"role": "user", "content": "Synthetic"}] * 129,
                "memory_excluded_indices": indices,
            }
        )
    assert any(item["loc"][:1] != ("messages",) for item in error.value.errors())
