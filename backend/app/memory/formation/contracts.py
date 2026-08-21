from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class MemoryFormationJob:
    character_id: str
    conversation_id: UUID
    turn_id: UUID

    def __post_init__(self) -> None:
        if not self.character_id.strip():
            raise ValueError("character_id must not be blank")
        if self.conversation_id.version != 4 or self.turn_id.version != 4:
            raise ValueError("conversation_id and turn_id must be UUID4")
