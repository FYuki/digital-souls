def parse_positive_integer(raw_value: str, key: str) -> int:
    if not raw_value.isascii() or not raw_value.isdecimal():
        raise ValueError(f"{key} must be a positive integer")
    value = int(raw_value)
    if value < 1 or str(value) != raw_value:
        raise ValueError(f"{key} must be a positive integer")
    return value
