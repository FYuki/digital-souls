"""入口に依存しない会話Coreの呼出情報。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.memory.formation.contracts import MemoryFormationJob


class ActorKind(StrEnum):
    OWNER = "owner"
    OTHER = "other"
    SELF = "self"


class OutputVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


@dataclass(frozen=True)
class CoreActor:
    kind: ActorKind
    platform: str
    external_id: str | None = None

    def __post_init__(self) -> None:
        if not self.platform.strip():
            raise ValueError("actor platform is required")
        if self.external_id is not None and not self.external_id.strip():
            raise ValueError("actor external_id must not be empty")


@dataclass(frozen=True)
class CoreInvocation:
    character_id: str
    conversation_id: UUID
    message: str
    actor: CoreActor
    output_visibility: OutputVisibility
    input_ids: tuple[str, ...] = ()
    response_id: str | None = None

    def __post_init__(self) -> None:
        if not self.character_id.strip():
            raise ValueError("character_id is required")
        if not self.message:
            raise ValueError("message is required")
        if any(not input_id for input_id in self.input_ids):
            raise ValueError("input_ids must not contain empty values")
        if self.response_id == "":
            raise ValueError("response_id must not be empty")

    def require_current_entry(self) -> None:
        """後続Epicの主体・公開範囲を既存入口から先行有効化しない。"""
        if self.actor.kind is not ActorKind.OWNER:
            raise ValueError("actor kind is not enabled for the current entries")
        if self.actor.external_id is not None:
            raise ValueError("actor external_id is not enabled for the current entries")
        if self.output_visibility is not OutputVisibility.PRIVATE:
            raise ValueError("public output requires the visibility policy")

    @classmethod
    def owner_web(
        cls,
        *,
        character_id: str,
        conversation_id: UUID,
        message: str,
        input_ids: tuple[str, ...] = (),
        response_id: str | None = None,
    ) -> CoreInvocation:
        return cls(
            character_id=character_id,
            conversation_id=conversation_id,
            message=message,
            actor=CoreActor(kind=ActorKind.OWNER, platform="web"),
            output_visibility=OutputVisibility.PRIVATE,
            input_ids=input_ids,
            response_id=response_id,
        )

    @classmethod
    def owner_voice(
        cls,
        *,
        character_id: str,
        conversation_id: UUID,
        message: str,
        input_ids: tuple[str, ...],
        response_id: str,
    ) -> CoreInvocation:
        return cls(
            character_id=character_id,
            conversation_id=conversation_id,
            message=message,
            actor=CoreActor(kind=ActorKind.OWNER, platform="voice"),
            output_visibility=OutputVisibility.PRIVATE,
            input_ids=input_ids,
            response_id=response_id,
        )


class MemoryFormationSubmitter(Protocol):
    def submit(self, job: MemoryFormationJob) -> None: ...


def submit_completed_turn(
    turn: ConversationTurn,
    *,
    screen_derived: bool,
    submitter: MemoryFormationSubmitter,
) -> None:
    """入口を問わず、保存済みの通常完了ターンだけを形成へ予約する。"""
    if turn.status is not TurnStatus.COMPLETED or screen_derived:
        return
    submitter.submit(
        MemoryFormationJob(
            character_id=turn.character_id,
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
        )
    )