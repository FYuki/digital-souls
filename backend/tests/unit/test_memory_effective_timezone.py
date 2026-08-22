import pytest


def test_memory_occurred_timezone_defaults_to_asia_tokyo(monkeypatch) -> None:
    from app.environment import iana_timezone_environment_value

    monkeypatch.delenv("MEMORY_OCCURRED_TIMEZONE", raising=False)

    assert (
        iana_timezone_environment_value(
            "MEMORY_OCCURRED_TIMEZONE",
            "Asia/Tokyo",
        )
        == "Asia/Tokyo"
    )


def test_memory_occurred_timezone_accepts_an_iana_name(monkeypatch) -> None:
    from app.environment import iana_timezone_environment_value

    monkeypatch.setenv("MEMORY_OCCURRED_TIMEZONE", "America/New_York")

    assert (
        iana_timezone_environment_value(
            "MEMORY_OCCURRED_TIMEZONE",
            "Asia/Tokyo",
        )
        == "America/New_York"
    )


@pytest.mark.parametrize(
    "value",
    ["", "Not/A_Timezone", "Asia/Tokyo ", "US/Eastern"],
)
def test_memory_occurred_timezone_rejects_invalid_or_unnormalized_names(
    value: str,
    monkeypatch,
) -> None:
    from app.environment import iana_timezone_environment_value

    monkeypatch.setenv("MEMORY_OCCURRED_TIMEZONE", value)

    with pytest.raises(ValueError, match="MEMORY_OCCURRED_TIMEZONE"):
        iana_timezone_environment_value(
            "MEMORY_OCCURRED_TIMEZONE",
            "Asia/Tokyo",
        )


def test_memory_occurred_timezone_uses_canonical_names_with_only_bundled_tzdata(
    monkeypatch,
) -> None:
    from app import environment

    monkeypatch.setattr(environment.zoneinfo, "TZPATH", ())
    monkeypatch.setattr(
        environment.zoneinfo,
        "available_timezones",
        lambda: {"Etc/UTC", "US/Eastern"},
    )
    monkeypatch.setattr(environment.zoneinfo, "ZoneInfo", lambda _value: object())
    monkeypatch.setattr(
        environment,
        "_bundled_tzdata_source",
        lambda: "Z Etc/UTC 0 - UTC\nL America/New_York US/Eastern\n",
    )
    environment._canonical_iana_timezone_names.cache_clear()

    try:
        monkeypatch.setenv("MEMORY_OCCURRED_TIMEZONE", "Etc/UTC")
        assert (
            environment.iana_timezone_environment_value(
                "MEMORY_OCCURRED_TIMEZONE",
                "Asia/Tokyo",
            )
            == "Etc/UTC"
        )

        monkeypatch.setenv("MEMORY_OCCURRED_TIMEZONE", "US/Eastern")
        with pytest.raises(ValueError, match="MEMORY_OCCURRED_TIMEZONE"):
            environment.iana_timezone_environment_value(
                "MEMORY_OCCURRED_TIMEZONE",
                "Asia/Tokyo",
            )
    finally:
        environment._canonical_iana_timezone_names.cache_clear()
