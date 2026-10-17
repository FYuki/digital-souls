"""Fixed operator-owned response contracts for managed local memory adapters."""

import hashlib
import json
from importlib.metadata import version
from typing import Any

from pydantic import BaseModel

CONTRACT_VERSION = "llamacpp-b11347-json-schema-v1"


def response_format(schema: type[BaseModel], name: str) -> dict[str, Any]:
    """Use the native Chat json_schema wrapper; never accept caller kwargs."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema.model_json_schema(),
        },
    }


def structured_provenance(schema: type[BaseModel], name: str) -> dict[str, str]:
    encoded = json.dumps(response_format(schema, name), sort_keys=True, separators=(",", ":"))
    return {
        "structured_output_contract": CONTRACT_VERSION,
        "response_schema_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "provider_sdk": f"litellm-{version('litellm')}/openai-{version('openai')}",
    }
