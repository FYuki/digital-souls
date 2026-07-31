import os


def positive_integer_environment_value(key: str, default: int) -> int:
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
