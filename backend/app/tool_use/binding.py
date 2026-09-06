"""管理側の対象定義と会話内の選択を、外部MCPのgrantから分離する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from app.external_mcp.models import Json, MCPFailure, encode


@dataclass(frozen=True)
class BindingTarget:
    id: str
    connection_id: str
    character_id: str
    label: str
    operations: tuple[str, ...]
    arguments_json: str = field(default="{}", repr=False)


class BindingResolver:
    def __init__(self, targets: tuple[BindingTarget, ...] = ()) -> None:
        self.targets = targets
        self._selected: dict[tuple[str, str, str, str], str] = {}
        self._resolved: dict[str, tuple[str, BindingTarget]] = {}

    def candidates(
        self, character: str, connection: str, operation: str
    ) -> tuple[BindingTarget, ...]:
        return tuple(
            t
            for t in self.targets
            if (
                t.character_id == character
                and t.connection_id == connection
                and operation in t.operations
            )
        )

    def resolve(
        self,
        character: str,
        conversation: str,
        connection: str,
        operation: str,
        *,
        explicit: str = "",
        required: bool = False,
    ) -> tuple[str | None, Json]:
        import json

        choices = self.candidates(character, connection, operation)
        key = (character, conversation, connection, operation)
        selected = explicit or self._selected.get(key, "")
        target = next((t for t in choices if t.id == selected), None)
        if explicit and target is None:
            raise MCPFailure("validation", "invalid_binding")
        if target is None and len(choices) == 1 and required:
            target = choices[0]
        if target is None:
            if required:
                raise MCPFailure("policy", "binding_input_required")
            return None, {}
        self._selected[key] = target.id
        # 呼出しIDを会話に固定し、LLMへvalidator用のIDを公開しない。
        existing = next(
            (
                key
                for key, value in self._resolved.items()
                if value == (conversation, target)
            ),
            None,
        )
        binding_id = existing or str(uuid4())
        self._resolved[binding_id] = (conversation, target)
        return binding_id, json.loads(target.arguments_json)

    async def validate(
        self,
        connection_id: str,
        operation: str,
        character_id: str,
        binding_id: str | None,
    ) -> bool:
        item = self._resolved.get(binding_id or "")
        return item is not None and item[1] in self.candidates(
            character_id, connection_id, operation
        )

    def forget(self, character: str, conversation: str) -> None:
        self._selected = {
            k: v
            for k, v in self._selected.items()
            if k[:2] != (character, conversation)
        }
        self._resolved = {
            k: v
            for k, v in self._resolved.items()
            if not (v[0] == conversation and v[1].character_id == character)
        }


def apply_binding(arguments: Json, constraints: Json) -> Json:
    if any(
        key in arguments and encode(arguments[key]) != encode(value)
        for key, value in constraints.items()
    ):
        raise MCPFailure("policy", "binding_argument_mismatch")
    return {**arguments, **constraints}
