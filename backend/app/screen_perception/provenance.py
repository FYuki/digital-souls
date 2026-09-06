from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal
from uuid import UUID


ScreenSource = Literal[
    "explicit_ui", "natural_language_text", "natural_language_voice"
]
ScreenSurface = Literal["monitor", "window"]
ScreenDerivation = Literal["direct_observation", "conversation_follow_up"]


@dataclass(frozen=True)
class ScreenLineage:
    """保存可能な画面由来metadata。画像・観測本文・対象名は含めない。"""

    screen_lineage_id: UUID
    origin_screen_session_id: UUID
    origin_generation: int
    origin_routing_revision: str
    source: ScreenSource
    surface: ScreenSurface
    derivation: ScreenDerivation = "direct_observation"

    def as_follow_up(self) -> "ScreenLineage":
        return replace(self, derivation="conversation_follow_up")

    def belongs_to(
        self, *, screen_session_id: UUID, generation: int, routing_revision: str
    ) -> bool:
        return (
            self.origin_screen_session_id == screen_session_id
            and self.origin_generation == generation
            and self.origin_routing_revision == routing_revision
        )
