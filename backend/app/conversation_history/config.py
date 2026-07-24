import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

STALE_AFTER_SECONDS_ENV = "CONVERSATION_TURN_STALE_AFTER_SECONDS"
RETENTION_DAYS_ENV = "CONVERSATION_HISTORY_RETENTION_DAYS"

DEFAULT_DATABASE_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "conversation-history.db"
)
DEFAULT_STALE_AFTER_SECONDS = 300
DEFAULT_RETENTION_DAYS = 365


@dataclass(frozen=True)
class ConversationHistoryConfig:
    database_path: Path
    stale_after: timedelta
    retention: timedelta


def _positive_integer_environment_value(key: str, default: int) -> int:
    raw_value = os.getenv(key)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if value < 1 or str(value) != raw_value:
        raise ValueError(f"{key} must be a positive integer")
    return value


def resolve_conversation_history_config() -> ConversationHistoryConfig:
    stale_seconds = _positive_integer_environment_value(
        STALE_AFTER_SECONDS_ENV,
        DEFAULT_STALE_AFTER_SECONDS,
    )
    retention_days = _positive_integer_environment_value(
        RETENTION_DAYS_ENV,
        DEFAULT_RETENTION_DAYS,
    )
    return ConversationHistoryConfig(
        database_path=DEFAULT_DATABASE_PATH,
        stale_after=timedelta(seconds=stale_seconds),
        retention=timedelta(days=retention_days),
    )
