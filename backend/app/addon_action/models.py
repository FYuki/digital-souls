"""承認の有効範囲。実引数やToolごとのpermissionとは分離する。"""

from dataclasses import dataclass
from enum import StrEnum


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
