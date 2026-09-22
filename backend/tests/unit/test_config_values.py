from __future__ import annotations

import pytest

from app.config_values import parse_positive_integer


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("1", 1),
        ("1024", 1024),
        ("99999999999999999999", 99999999999999999999),
    ],
)
def test_parse_positive_integer_accepts_canonical_ascii_decimal(
    raw_value: str,
    expected: int,
) -> None:
    assert parse_positive_integer(raw_value, "TEST_SETTING") == expected


@pytest.mark.parametrize(
    "raw_value",
    [
        "",
        "0",
        "-1",
        "01",
        " 1",
        "1 ",
        "１２",
        "+1",
        "1.5",
        "invalid",
    ],
)
def test_parse_positive_integer_rejects_non_positive_or_noncanonical_values(
    raw_value: str,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_positive_integer(raw_value, "TEST_SETTING")

    assert str(exc_info.value) == "TEST_SETTING must be a positive integer"
