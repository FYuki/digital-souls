"""関連Epicの接続契約。未実装の正本更新を成功に見せない。"""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from .models import ReflectionView, Result


class MemoryPort(Protocol):
    """run_idを冪等keyとし、既存keyは同じ経験へ解決する。SELF正本の更新条件は#100が所有する。"""

    async def record_observation(
        self,
        *,
        character: str,
        run_id: str,
        experienced_at: datetime,
        topic: str,
        source_revisions: tuple[str, ...],
    ) -> Result: ...
    async def catch_up(self, character: str) -> Result: ...


class PersonalityPort(Protocol):
    """request_idで重複排除し、独立証拠・閾値・cooldownが満たされる場合だけ#101が更新する。"""

    async def evaluate(self, character: str, request_id: str) -> Result: ...


class DeferredMemory:
    async def record_observation(
        self,
        *,
        character: str,
        run_id: str,
        experienced_at: datetime,
        topic: str,
        source_revisions: tuple[str, ...],
    ) -> Result:
        return Result.DEFERRED

    async def catch_up(self, character: str) -> Result:
        return Result.DEFERRED


class DeferredPersonality:
    async def evaluate(self, character: str, request_id: str) -> Result:
        return Result.DEFERRED


class ReflectionSource(Protocol):
    """activeは当該characterの形成対象batch（最大16件）。revisionは訂正・非公開化で更新する。

    current_revisionsは完全なACTIVE集合について本文を読まない同期の正本照合。Noneは未接続・取得不能で、
    派生状態を会話・外部実行へ採用しない。#100は変更時にStore.invalidate_reflectionsも通知する。
    """

    async def active(self, character: str) -> tuple[ReflectionView, ...] | None: ...
    def current_revisions(self, character: str) -> dict[UUID, str] | None: ...


class DeferredReflections:
    async def active(self, character: str) -> tuple[ReflectionView, ...] | None:
        return None

    def current_revisions(self, character: str) -> dict[UUID, str] | None:
        return None
