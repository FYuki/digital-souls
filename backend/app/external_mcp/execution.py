from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from .models import Json, MCPFailure, encode


@dataclass(frozen=True)
class ValidatedExecutionInput:
    """JSON再構築と検証を通過した、1回のGate実行入力。"""

    original_arguments: Json
    arguments: Json
    responses: Json | None


def normalize_execution_input(
    arguments: Json, responses: Json | None
) -> ValidatedExecutionInput:
    try:
        normalized_arguments = json.loads(encode(arguments))
        normalized_responses = (
            json.loads(encode(responses))
            if responses is not None
            else None
        )
    except (ValueError, TypeError):
        raise MCPFailure("validation", "invalid_arguments") from None
    return ValidatedExecutionInput(
        original_arguments=normalized_arguments,
        arguments=normalized_arguments,
        responses=normalized_responses,
    )


@dataclass(frozen=True)
class ExecutionPreparation:
    """再認可とdispatchの直前に共有する検証済み実行準備。"""

    connection_id: str
    operation: str
    kind: Literal["tool", "resource"]
    arguments: Json
    responses: Json | None
    binding_id: str | None
    generation: int
    policy: Json
    effective: Json
    rules: frozenset[str]
    parallel: bool
    retry: bool
