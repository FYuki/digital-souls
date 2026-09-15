"""後続Issueへ渡す境界。許可判断やTaskを自動で実装しない。"""

from __future__ import annotations
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .gate import ExecutionContext
    from app.addon_events.runtime import Delivery
from collections.abc import Callable
from app.addon_action.models import ActionInvocation, ApprovalTicket
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
        session_id: str,
    ) -> bool: ...


class ConfirmationPolicyPort(Protocol):
    async def prepare(
        self,
        call: ActionInvocation,
        *,
        live: Callable[[], None],
        request_id: str | None = None,
    ) -> ApprovalTicket: ...
    async def validate_egress(self, arguments: Json) -> None: ...
    def validate_background_read(self, connection_id: str, identity: str) -> None: ...
    def consume(self, ticket: ApprovalTicket) -> bool: ...
    def end_loop(self, loop_id: str) -> None: ...
    def end_wait(self, request_id: str) -> None: ...


class TaskTrackerPort(Protocol):
    async def status(self, connection_id: str, task_id: str) -> str: ...
    async def cancel(self, connection_id: str, task_id: str) -> str: ...


class EventSourcePort(Protocol):
    async def subscribe(self, source_id: str, consumer_id: str, context: ExecutionContext) -> str: ...
    async def read(self, source_id: str, consumer_id: str, context: ExecutionContext) -> Delivery: ...
    async def acknowledge(self, source_id: str, consumer_id: str, receipt: str, context: ExecutionContext, *, accept_gap: bool = False) -> None: ...
    async def unsubscribe(self, source_id: str, consumer_id: str, context: ExecutionContext) -> None: ...


class ActionRecoveryPort(Protocol):
    async def recover(self, execution_id: str) -> str: ...


class UnsupportedTasks:
    async def status(self, connection_id: str, task_id: str) -> str:
        return "unsupported"

    async def cancel(self, connection_id: str, task_id: str) -> str:
        return "unsupported"
