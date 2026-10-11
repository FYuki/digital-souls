import pytest

from digital_souls_core.conversations import request_fingerprint

from .conversation_support import turn
from .test_llamacpp_provider import local_character

pytestmark = pytest.mark.it1


def test_local_endpoint_is_part_of_retry_identity() -> None:
    config = local_character().config
    other = config.model_copy(
        update={
            "profile": config.profile.model_copy(update={"api_base": "http://127.0.0.1:18082/v1"})
        }
    )
    assert request_fingerprint(turn(), config) != request_fingerprint(turn(), other)


def test_default_estimate_preserves_legacy_sdk_retry_identity() -> None:
    from .support import character

    assert request_fingerprint(turn(), character("synthetic").config) == (
        "a7339e54ce565cabc49e4ae4e5332ec0b9391a54bd3d19219277855a60399487"
    )


def test_local_counter_selection_is_part_of_retry_identity() -> None:
    config = local_character().config
    other = config.model_copy(
        update={"profile": config.profile.model_copy(update={"token_counter": "llamacpp"})}
    )
    assert request_fingerprint(turn(), config) != request_fingerprint(turn(), other)
