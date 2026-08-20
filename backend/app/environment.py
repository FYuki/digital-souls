import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError


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


def iana_timezone_environment_value(key: str, default: str) -> str:
    raw_value = os.getenv(key)
    value = default if raw_value is None else raw_value
    try:
        ZoneInfo(value)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"{key} must be an IANA timezone name") from exc
    if value not in _canonical_iana_timezone_names():
        raise ValueError(f"{key} must be an IANA timezone name")
    return value


@lru_cache(maxsize=1)
def _canonical_iana_timezone_names() -> frozenset[str]:
    names: set[str] = set()
    for root in map(Path, TZPATH):
        tzdata_source = root / "tzdata.zi"
        if not tzdata_source.is_file():
            continue
        for line in tzdata_source.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] in {"Z", "Zone"}:
                names.add(fields[1])
    if names:
        return frozenset(names)

    for root in map(Path, TZPATH):
        zone_table = root / "zone.tab"
        if not zone_table.is_file():
            continue
        for line in zone_table.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                fields = line.split("\t")
                if len(fields) >= 3:
                    names.add(fields[2])
    return frozenset(names)
