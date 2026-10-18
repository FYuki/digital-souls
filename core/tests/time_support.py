from datetime import UTC, datetime, timedelta

FIRST = datetime(2024, 2, 29, 12, 34, 56, 123456, tzinfo=UTC)

SECOND = FIRST + timedelta(days=2)
