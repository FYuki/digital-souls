"""承認の有効範囲。実引数やToolごとのpermissionとは分離する。"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class OperationGroup(StrEnum):
    NORMAL = "normal"
    HIGH_IMPACT = "high_impact"


class ExecutionScene(StrEnum):
    CONVERSATION = "conversation"
    AUTONOMOUS = "autonomous"


class ApprovalChoice(StrEnum):
    ALWAYS = "always"
    ONCE = "once"
    REJECT = "reject"


class Permission(StrEnum):
    UNAPPROVED = "unapproved"
    ALWAYS = "always"
    DENIED = "denied"


@dataclass(frozen=True)
class ApprovalKey:
    connection_id: str
    connection_identity: str
    group: OperationGroup
    scene: ExecutionScene

    def values(self) -> tuple[str, str, str, str]:
        return (self.connection_id, self.connection_identity, self.group, self.scene)


@dataclass(frozen=True)
class ActionInvocation:
    connection_id: str
    connection_identity: str
    connection_label: str
    character_id: str
    session_id: str
    loop_id: str
    scene: ExecutionScene
    operation: str
    static_impact: dict[str, Any] = field(repr=False)
    arguments: dict[str, Any] = field(repr=False)
    binding_id: str | None = None
    force_confirmation: bool = False
    input_responses: dict[str, Any] | None = field(default=None, repr=False)
    core_argument_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovalTicket:
    key: ApprovalKey
    request_id: str | None = None
