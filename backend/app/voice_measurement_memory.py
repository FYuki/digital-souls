"""専用音声測定だけで記憶形成・統合の負荷を除外する。"""
from collections.abc import Mapping
import json
from pathlib import Path

from app.memory.formation.contracts import MemoryFormationJob

DISABLE_FORMATION_ENV = "VOICE_MEASUREMENT_DISABLE_MEMORY_FORMATION"
POLICY_PATH = Path("voice-metrics/memory-policy.json")


def formation_disabled(environment: Mapping[str, str], *, environment_id: str,
                       measurement_kind: str) -> bool:
    value = environment.get(DISABLE_FORMATION_ENV, "false")
    if value not in ("true", "false"):
        raise ValueError(f"{DISABLE_FORMATION_ENV} must be true or false")
    if value == "true" and (environment_id != "test" or measurement_kind != "controlled_baseline"):
        raise ValueError("memory formation isolation requires a controlled test measurement")
    return value == "true"


class DisabledFormationScheduler:
    """測定中は会話完了通知を受けても形成・永続予約の回復を実行しない。"""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def submit(self, job: MemoryFormationJob) -> None:
        pass

    def is_busy(self) -> bool:
        return False


def record_memory_policy(data_root: Path, *, disabled: bool) -> None:
    path = data_root / POLICY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "method": "controlled_memory_schedulers_v1",
        "formation_disabled": disabled,
        "consolidation_disabled": disabled,
    }, sort_keys=True) + "\n")
