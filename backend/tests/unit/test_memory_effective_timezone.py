import pytest


def test_memory_effective_timezone_defaults_to_asia_tokyo(monkeypatch) -> None:
    from app.environment import iana_timezone_environment_value

    monkeypatch.delenv("MEMORY_EFFECTIVE_TIMEZONE", raising=False)

    assert (
        iana_timezone_environment_value(
            "MEMORY_EFFECTIVE_TIMEZONE",
            "Asia/Tokyo",
        )
        == "Asia/Tokyo"
    )


def test_memory_effective_timezone_accepts_an_iana_name(monkeypatch) -> None:
    from app.environment import iana_timezone_environment_value

    monkeypatch.setenv("MEMORY_EFFECTIVE_TIMEZONE", "America/New_York")

    assert (
        iana_timezone_environment_value(
            "MEMORY_EFFECTIVE_TIMEZONE",
            "Asia/Tokyo",
        )
        == "America/New_York"
    )


@pytest.mark.parametrize(
    "value",
    ["", "Not/A_Timezone", "Asia/Tokyo "],
)
def test_memory_effective_timezone_rejects_invalid_or_unnormalized_names(
    value: str,
    monkeypatch,
) -> None:
    from app.environment import iana_timezone_environment_value

    monkeypatch.setenv("MEMORY_EFFECTIVE_TIMEZONE", value)

    with pytest.raises(ValueError, match="MEMORY_EFFECTIVE_TIMEZONE"):
        iana_timezone_environment_value(
            "MEMORY_EFFECTIVE_TIMEZONE",
            "Asia/Tokyo",
        )
