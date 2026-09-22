import os
import re
import zoneinfo
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib import resources
from pathlib import Path


_DECIMAL_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


def positive_integer_value(raw_value: str, key: str) -> int:
    """canonicalな10進表記の正整数文字列のみを受理する。"""
    if not raw_value.isascii() or not raw_value.isdecimal():
        raise ValueError(f"{key} must be a positive integer")
    value = int(raw_value)
    if value < 1 or str(value) != raw_value:
        raise ValueError(f"{key} must be a positive integer")
    return value


def positive_integer_environment_value(key: str, default: int) -> int:
    raw_value = os.getenv(key)
    if raw_value is None:
        return default
    return positive_integer_value(raw_value, key)


def positive_integer_setting(
    environment: Mapping[str, str], key: str, default: int
) -> int:
    raw_value = environment.get(key)
    return default if raw_value is None else positive_integer_value(raw_value, key)


def positive_decimal_value(raw_value: str, key: str) -> float:
    """canonicalな10進表記の正の実数文字列のみを受理する。"""
    if _DECIMAL_PATTERN.fullmatch(raw_value) is None:
        raise ValueError(f"{key} must be a positive decimal")
    try:
        value = Decimal(raw_value)
    except InvalidOperation:
        raise ValueError(f"{key} must be a positive decimal") from None
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{key} must be a positive decimal")
    return float(value)


def positive_decimal_setting(
    environment: Mapping[str, str], key: str, default: float
) -> float:
    raw_value = environment.get(key)
    return default if raw_value is None else positive_decimal_value(raw_value, key)


def iana_timezone_environment_value(key: str, default: str) -> str:
    raw_value = os.getenv(key)
    value = default if raw_value is None else raw_value
    try:
        zoneinfo.ZoneInfo(value)
    except (TypeError, ValueError, zoneinfo.ZoneInfoNotFoundError) as exc:
        raise ValueError(f"{key} must be an IANA timezone name") from exc
    if value not in _canonical_iana_timezone_names():
        raise ValueError(f"{key} must be an IANA timezone name")
    return value


@lru_cache(maxsize=1)
def _canonical_iana_timezone_names() -> frozenset[str]:
    names: set[str] = set()
    for root in map(Path, zoneinfo.TZPATH):
        tzdata_source = root / "tzdata.zi"
        if not tzdata_source.is_file():
            continue
        try:
            source = tzdata_source.read_text(encoding="utf-8")
        except OSError:
            continue
        names.update(_canonical_names_from_tzdata_source(source))

    bundled_source = _bundled_tzdata_source()
    if bundled_source is not None:
        names.update(_canonical_names_from_tzdata_source(bundled_source))

    return frozenset(zoneinfo.available_timezones().intersection(names))


def _canonical_names_from_tzdata_source(source: str) -> set[str]:
    names: set[str] = set()
    for line in source.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] in {"Z", "Zone"}:
            names.add(fields[1])
    return names


def _bundled_tzdata_source() -> str | None:
    try:
        source = resources.files("tzdata.zoneinfo").joinpath("tzdata.zi")
        return source.read_text(encoding="utf-8")
    except (ImportError, OSError):
        return None
