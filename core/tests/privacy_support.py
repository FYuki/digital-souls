import json
from typing import Any

from digital_souls_core.character import AccessScope, Profile
from digital_souls_core.history import Binding
from digital_souls_core.privacy_scan import POLICY_VERSION

BINDING = Binding(AccessScope(), "synthetic")


def local_profile() -> Profile:
    return Profile(
        profile_id="privacy-local",
        model="openai/gemma4-12b",
        transport="llamacpp_chat",
        api_base="http://127.0.0.1:18080/v1",
        external_send_allowed=True,
        allowed_parameters=frozenset({"stream", "tools", "max_completion_tokens"}),
    )


def assessment(**changes: Any) -> str:
    return json.dumps(
        {
            "classification": "NOT_SENSITIVE",
            "subject": "GENERAL",
            "category": "NONE",
            "policy_version": POLICY_VERSION,
            **changes,
        }
    )
