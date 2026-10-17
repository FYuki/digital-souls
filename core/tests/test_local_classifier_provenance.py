import pytest

from digital_souls_core.privacy_classifier import LocalClassifier

from .privacy_support import local_profile
from .support import FakeProvider

pytestmark = pytest.mark.it1


def test_destination_identity_is_whitelisted_and_secret_free() -> None:
    profile = local_profile().model_copy(update={"api_base": "HTTP://127.0.0.1:018080/v1"})
    instance = LocalClassifier(FakeProvider(), profile, model_digest="synthetic")
    provenance = instance.provenance
    assert provenance["endpoint"] == "http://127.0.0.1:18080/v1"
    assert provenance["transport"] == "llamacpp_chat"
    assert provenance["profile_id"] == profile.profile_id
    assert not any(key in provenance for key in ("api_key", "token", "password", "headers"))
    for endpoint in (
        "http://synthetic:secret@127.0.0.1:18080/v1",
        "http://127.0.0.1:18080/v1?token=synthetic",
        "http://127.0.0.1:18080/v1#synthetic",
    ):
        invalid = local_profile().model_copy(update={"api_base": endpoint})
        with pytest.raises(ValueError):
            type(instance)(FakeProvider(), invalid, model_digest="synthetic")
