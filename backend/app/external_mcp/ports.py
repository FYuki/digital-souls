"""後続Issueへ渡す境界。許可判断やTaskを自動で実装しない。"""

from __future__ import annotations
from typing import Protocol
from .models import Discovery, Json


class SecretResolverPort(Protocol):
    async def resolve(self, secret_ref: str) -> str: ...


class CapabilitySource(Protocol):
    @property
    def connected(self) -> bool: ...
    async def discover(self) -> Discovery: ...
    async def call_tool(
        self,
        name: str,
        arguments: Json,
        *,
        input_responses: Json | None = None,
        request_state: str | None = None,
    ) -> Json: ...
    async def read_resource(
        self,
        uri: str,
        *,
        input_responses: Json | None = None,
        request_state: str | None = None,
    ) -> Json: ...


class BindingValidatorPort(Protocol):
    async def validate(
        self,
        connection_id: str,
        operation: str,
        character_id: str,
        binding_id: str | None,
    ) -> bool: ...


class ConfirmationPolicyPort(Protocol):
    async def confirmed(
        self, connection_id: str, operation: str, arguments: Json, character_id: str
    ) -> bool: ...


class TaskTrackerPort(Protocol):
    async def status(self, connection_id: str, task_id: str) -> str: ...
    async def cancel(self, connection_id: str, task_id: str) -> str: ...


class EventSourcePort(Protocol):
    async def subscribe(self, connection_id: str, cursor: str | None) -> str: ...


class ActionRecoveryPort(Protocol):
    async def recover(self, execution_id: str) -> str: ...


class UnsupportedTasks:
    async def status(self, connection_id: str, task_id: str) -> str:
        return "unsupported"

    async def cancel(self, connection_id: str, task_id: str) -> str:
        return "unsupported"
