"""関連Epicの接続契約。未実装の正本更新を成功に見せない。"""

from datetime import datetime
from typing import Protocol

from .models import Result


class MemoryPort(Protocol):
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


from .models import ReflectionView


class ReflectionSource(Protocol):
    async def active(self, character: str) -> tuple[ReflectionView, ...] | None: ...


class DeferredReflections:
    async def active(self, character: str) -> tuple[ReflectionView, ...] | None:
        return None
