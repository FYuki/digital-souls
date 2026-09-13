"""好みの既存形成とEpisodeの永続形成を同じ会話通知へ接続する。"""

from typing import Protocol

from app.memory.formation.contracts import MemoryFormationJob


class FormationScheduler(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def submit(self, job: MemoryFormationJob) -> None: ...
    def is_busy(self) -> bool: ...


class CombinedFormationScheduler:
    def __init__(self, preference: FormationScheduler, episodic: FormationScheduler) -> None:
        self._preference = preference
        self._episodic = episodic

    async def start(self) -> None:
        await self._preference.start()
        try:
            await self._episodic.start()
        except BaseException:
            await self._preference.stop()
            raise

    def submit(self, job: MemoryFormationJob) -> None:
        try:
            self._preference.submit(job)
        finally:
            # 既存のキューが失敗しても、永続予約の起床通知は届ける。
            self._episodic.submit(job)

    def is_busy(self) -> bool:
        return self._preference.is_busy() or self._episodic.is_busy()

    async def stop(self) -> None:
        try:
            await self._episodic.stop()
        finally:
            await self._preference.stop()
