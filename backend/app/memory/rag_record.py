from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

MemoryRole = Literal["user"]


@dataclass(frozen=True)
class MemoryCandidateRecord:
    id: str
    character: str
    role: MemoryRole
    content: str
    timestamp: str


def create_memory_candidate_record(
    character: str,
    content: str,
) -> MemoryCandidateRecord:
    return MemoryCandidateRecord(
        id=str(uuid4()),
        character=character,
        role="user",
        content=content,
        timestamp=datetime.now(UTC).isoformat(),
    )
