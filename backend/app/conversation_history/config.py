from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from app.environment import positive_integer_environment_value

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


def resolve_conversation_history_config() -> ConversationHistoryConfig:
    stale_seconds = positive_integer_environment_value(
        STALE_AFTER_SECONDS_ENV,
        DEFAULT_STALE_AFTER_SECONDS,
    )
    retention_days = positive_integer_environment_value(
        RETENTION_DAYS_ENV,
        DEFAULT_RETENTION_DAYS,
    )
    return ConversationHistoryConfig(
        database_path=DEFAULT_DATABASE_PATH,
        stale_after=timedelta(seconds=stale_seconds),
        retention=timedelta(days=retention_days),
    )
