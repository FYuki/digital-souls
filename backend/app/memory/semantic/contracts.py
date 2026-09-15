"""形成経路・命題・各根拠版・適用時期を分離した共通契約。"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.memory.episodic.contracts import (
    Contract, FormationStamp, Identity, ResolvedTime, SourceSpan, Text, Version,
)


class FormationType(str, Enum):
    DIRECT_EXTRACTION = "DIRECT_EXTRACTION"
    EXPERIENCE_DERIVED = "EXPERIENCE_DERIVED"


class SemanticStatus(str, Enum):
    ACTIVE = "ACTIVE"
    HISTORICAL = "HISTORICAL"
    SUPERSEDED = "SUPERSEDED"
    CONFLICTED = "CONFLICTED"
    INACTIVE = "INACTIVE"
    DELETED = "DELETED"


class SemanticOperation(str, Enum):
    NEW = "NEW"
    REAFFIRM = "REAFFIRM"
    CORRECT = "CORRECT"
    CHANGE = "CHANGE"
    CONFLICT = "CONFLICT"
    SELF_REPORT = "SELF_REPORT"


class SemanticSource(Contract):
    kind: Literal["CONVERSATION", "EPISODE", "MANUAL"]
    source_id: UUID
    revision: Version
    conversation_id: UUID | None = None
    span: SourceSpan | None = None

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.kind == "CONVERSATION":
            if self.conversation_id is None or self.span is None:
                raise ValueError("conversation provenance requires conversation and span")
            if (self.span.source_id != self.source_id or self.span.revision != self.revision
                    or self.span.role not in {"user", "assistant"}):
                raise ValueError("inconsistent conversation provenance")
        elif self.span is not None or self.conversation_id is not None:
            raise ValueError("non-conversation provenance cannot contain conversation fields")
        return self

    @property
    def identity(self) -> str:
        return self.model_dump_json()


class Proposition(Contract):
    subject: Text
    predicate: Text
    value: Text
    content: Annotated[str, Field(strict=True, min_length=1, max_length=1000)]
    mutability: Literal["CHANGEABLE", "FIXED"]
    self_report: Annotated[bool, Field(strict=True)]
    valid_from: ResolvedTime | None = None
    valid_until: ResolvedTime | None = None


class SemanticCandidate(Contract):
    formation_type: FormationType
    proposition: Proposition
    sources: Annotated[tuple[SemanticSource, ...], Field(min_length=1, max_length=64)]
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]

    @model_validator(mode="after")
    def evidence(self) -> Self:
        if len({s.identity for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate semantic provenance")
        if self.formation_type is FormationType.DIRECT_EXTRACTION:
            if any(s.kind == "EPISODE" for s in self.sources):
                raise ValueError("direct extraction requires direct evidence")
            if not any(s.kind == "MANUAL" or (s.span and s.span.role == "user") for s in self.sources):
                raise ValueError("assistant output alone cannot ground semantic memory")
        else:
            if any(s.kind != "EPISODE" for s in self.sources):
                raise ValueError("derived semantic requires Episode evidence")
            if len({s.source_id for s in self.sources}) < 2:
                raise ValueError("derived semantic requires multiple Episodes")
            if self.proposition.self_report:
                raise ValueError("generalization is not self report")
        return self


class SemanticRecord(Contract):
    id: UUID
    character_id: Identity
    formation_type: FormationType
    content_version: Version
    status: SemanticStatus
    proposition: Proposition | None
    sources: tuple[SemanticSource, ...]
    confidence: float
    stamp: FormationStamp
    created_at: datetime
    updated_at: datetime


class SemanticRelation(Contract):
    character_id: Identity
    source_id: UUID
    source_version: Version
    target_id: UUID
    target_version: Version
    relation: SemanticOperation
    created_at: datetime
