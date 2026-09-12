"""抽出器が提案できる操作と出典。ID・日時・正本の所有権は生成させない。"""

from typing import Annotated, Literal, Self
from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

from app.memory.episodic.contracts import Contract, ExtractedFiveW, RecordKind, Version

LocalKey = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class SourceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID4
    revision: Version
    role: Literal["user", "assistant"]
    quote: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
    # 省略時は入力中で一意に定位できる引用だけを受理する。
    start: Annotated[int, Field(strict=True, ge=0)] | None = None


class ExistingTarget(Contract):
    id: UUID4
    version: Version


class ExtractedRecord(Contract):
    key: LocalKey
    kind: RecordKind
    operation: Literal["NEW", "CONTINUE", "UPDATE", "REFERENCE"]
    target: ExistingTarget | None = None
    five_w: ExtractedFiveW | None = None
    anchor: SourceQuote
    sources: Annotated[tuple[SourceQuote, ...], Field(min_length=1, max_length=32)]
    time_source: SourceQuote | None = None
    changes: Annotated[tuple[Literal["who", "what", "when", "where", "why", "context"], ...],
                       Field(max_length=6)] = ()

    @model_validator(mode="after")
    def operation_contract(self) -> Self:
        if (self.operation == "NEW") != (self.target is None):
            raise ValueError("only NEW may omit a target")
        if (self.operation == "REFERENCE") != (self.five_w is None):
            raise ValueError("REFERENCE must not modify content")
        if self.operation in {"UPDATE", "CONTINUE"} and not self.changes:
            raise ValueError("content updates require explicit changed fields")
        if self.operation in {"NEW", "REFERENCE"} and self.changes:
            raise ValueError("only content updates may select changed fields")
        if self.operation == "CONTINUE" and self.kind is not RecordKind.EPISODE:
            raise ValueError("CONTINUE is an Episode operation")
        if self.operation in {"UPDATE", "REFERENCE"} and self.kind is not RecordKind.FACT:
            raise ValueError("UPDATE and REFERENCE are Fact operations")
        if self.five_w is not None and self.five_w.when is not None and self.time_source is None:
            raise ValueError("explicit temporal information requires its source")
        return self


class ExtractedLink(Contract):
    episode: LocalKey
    fact: LocalKey
    sources: Annotated[tuple[SourceQuote, ...], Field(min_length=1, max_length=32)]


class ExtractedMerge(Contract):
    source: LocalKey
    target: LocalKey
    evidence: Annotated[tuple[SourceQuote, ...], Field(min_length=1, max_length=32)]
    same_event: Literal[True]

    @field_validator("same_event", mode="before")
    @classmethod
    def require_true(cls, value: object) -> object:
        if value is not True:
            raise ValueError("same_event requires an explicit boolean")
        return value


class ExtractionBatch(Contract):
    complete: Annotated[bool, Field(strict=True)] = True
    records: Annotated[tuple[ExtractedRecord, ...], Field(max_length=32)]
    links: Annotated[tuple[ExtractedLink, ...], Field(max_length=64)] = ()
    merges: Annotated[tuple[ExtractedMerge, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def local_references(self) -> Self:
        by_key = {record.key: record for record in self.records}
        if len(by_key) != len(self.records):
            raise ValueError("duplicate local record key")
        target_ids = [r.target.id for r in self.records if r.target is not None]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("combine operations for each existing target")
        for link in self.links:
            if (
                link.episode not in by_key or link.fact not in by_key
                or by_key[link.episode].kind is not RecordKind.EPISODE
                or by_key[link.fact].kind is not RecordKind.FACT
            ):
                raise ValueError("invalid Episode-Fact reference")
        for merge in self.merges:
            if (
                merge.source == merge.target or merge.source not in by_key or merge.target not in by_key
                or by_key[merge.source].kind is not RecordKind.FACT
                or by_key[merge.target].kind is not RecordKind.FACT
            ):
                raise ValueError("invalid Fact merge")
        return self
