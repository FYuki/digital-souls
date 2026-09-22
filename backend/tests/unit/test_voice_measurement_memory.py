import pytest
from app.voice_measurement_memory import DISABLE_FORMATION_ENV, formation_disabled


@pytest.mark.parametrize("environment_id,kind", [
    ("dev", "controlled_baseline"), ("dogfood", "controlled_baseline"),
    ("test", "automated_test"), ("test", "dogfood"),
])
def test_formation_isolation_cannot_disable_normal_runtime(environment_id, kind):
    with pytest.raises(ValueError, match="controlled test"):
        formation_disabled({DISABLE_FORMATION_ENV: "true"},
                           environment_id=environment_id, measurement_kind=kind)


@pytest.mark.parametrize("value", ["", "1", "TRUE", "invalid"])
def test_invalid_formation_isolation_is_rejected(value):
    with pytest.raises(ValueError, match="true or false"):
        formation_disabled({DISABLE_FORMATION_ENV: value},
                           environment_id="test", measurement_kind="controlled_baseline")


def test_normal_runtime_defaults_to_formation_enabled():
    assert formation_disabled({}, environment_id="dogfood", measurement_kind="dogfood") is False
    assert formation_disabled({DISABLE_FORMATION_ENV: "false"},
                              environment_id="test", measurement_kind="controlled_baseline") is False
    assert formation_disabled({DISABLE_FORMATION_ENV: "true"},
                              environment_id="test", measurement_kind="controlled_baseline") is True
