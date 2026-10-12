"""Storage-independent finite formation contracts; no extraction implementation."""

from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol

from pydantic import ConfigDict, Field

from .application import CoreError
from .contracts import Message, StrictModel
from .history import Binding, HistoryStore, SourceState
from .privacy import PrivacyPolicy


class FormationConfig(StrictModel):
    """Trusted finite limits; values are provisional operational defaults."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    max_jobs: Annotated[int, Field(ge=1, le=100)] = 10
    lease_seconds: Annotated[int, Field(ge=1, le=3600)] = 300
    processor_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 60
    max_attempts: Annotated[int, Field(ge=1, le=100)] = 3
    retry_backoff_seconds: Annotated[int, Field(ge=0, le=3600)] = 60


def job_limit(config: FormationConfig, max_jobs: int | None) -> int:
    if max_jobs is None:
        return config.max_jobs
    if type(max_jobs) is not int or not 1 <= max_jobs <= config.max_jobs:
        raise CoreError(400, "formation_limit", "Invalid formation job limit")
    return max_jobs


@dataclass(frozen=True)
class FormationSource:
    state: SourceState
    message: Message = field(repr=False)


@dataclass(frozen=True)
class FormationInput:
    """Reservation origin, not the processor's matching or read scope.

    Processors choose additional reads through their injected authorized ports.
    Origin sources are an eligible trigger snapshot, not a complete search scope.
    """

    origin_binding: Binding
    origin_conversation_id: str
    origin_revision: int
    origin_memory_epoch: int
    origin_sources: tuple[FormationSource, ...] = field(repr=False)


class FormationProcessor(Protocol):
    """Trusted #209 seam: choose read scope, validate provenance before writes.

    Restart from the beginning, write idempotently and honor cancellation. Returning
    normally acknowledges this input; exceptions never become public text.
    """

    async def process(self, job: FormationInput) -> None: ...


@dataclass(frozen=True)
class DrainResult:
    succeeded: int = 0
    failed: int = 0
    excluded: int = 0

    @property
    def status(self) -> Literal["success", "failure"]:
        return "failure" if self.failed else "success"


class FormationDrain(Protocol):
    @property
    def history(self) -> HistoryStore: ...
    @property
    def policy(self) -> PrivacyPolicy: ...
    @property
    def config(self) -> FormationConfig: ...
    async def drain(self, *, max_jobs: int | None = None) -> DrainResult: ...
