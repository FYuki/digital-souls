from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import re
from types import MappingProxyType
from typing import cast

from app.inference.contracts import (
    InferenceCapability,
    InferenceTarget,
    JsonValue,
    ProviderKind,
    ProviderReference,
    ResolvedTarget,
    TargetCriticality,
    TargetDefinition,
    TargetFailurePolicy,
)
from app.inference.registry import ProviderRegistry


INFERENCE_TARGET_PREFIX = "INFERENCE_TARGET_"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CONCURRENCY = 1
_SUFFIXES = (
    "",
    "_OPTIONS_JSON",
    "_MAX_INPUT_TOKENS",
    "_MAX_OUTPUT_TOKENS",
    "_TIMEOUT_SECONDS",
    "_MAX_CONCURRENCY",
)
_DECIMAL_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


TARGET_DEFINITIONS: Mapping[InferenceTarget, TargetDefinition] = {
    InferenceTarget.CHAT: TargetDefinition(
        target=InferenceTarget.CHAT,
        env_token="CHAT",
        required_capabilities=frozenset(
            {
                InferenceCapability.GENERATE_TEXT,
                InferenceCapability.STREAM_TEXT,
                InferenceCapability.ESTIMATE_INPUT_TOKENS,
            }
        ),
        criticality=TargetCriticality.REQUIRED,
        failure_policy=TargetFailurePolicy.CHAT_ERROR,
        requires_output_limit=True,
    ),
    InferenceTarget.PRIVACY: TargetDefinition(
        target=InferenceTarget.PRIVACY,
        env_token="PRIVACY",
        required_capabilities=frozenset(
            {
                InferenceCapability.GENERATE_STRUCTURED,
                InferenceCapability.ESTIMATE_INPUT_TOKENS,
            }
        ),
        criticality=TargetCriticality.DEGRADABLE,
        failure_policy=TargetFailurePolicy.PRIVACY_ABSTAIN,
        requires_output_limit=True,
        local_only=True,
    ),
    InferenceTarget.MEMORY_EXTRACTION: TargetDefinition(
        target=InferenceTarget.MEMORY_EXTRACTION,
        env_token="MEMORY_EXTRACTION",
        required_capabilities=frozenset(
            {
                InferenceCapability.GENERATE_STRUCTURED,
                InferenceCapability.ESTIMATE_INPUT_TOKENS,
            }
        ),
        criticality=TargetCriticality.DEGRADABLE,
        failure_policy=TargetFailurePolicy.WORKER_RETRY,
        requires_output_limit=True,
    ),
    InferenceTarget.MEMORY_CONSOLIDATION: TargetDefinition(
        target=InferenceTarget.MEMORY_CONSOLIDATION,
        env_token="MEMORY_CONSOLIDATION",
        required_capabilities=frozenset(
            {
                InferenceCapability.GENERATE_STRUCTURED,
                InferenceCapability.ESTIMATE_INPUT_TOKENS,
            }
        ),
        criticality=TargetCriticality.DEGRADABLE,
        failure_policy=TargetFailurePolicy.NOOP,
        requires_output_limit=True,
    ),
    InferenceTarget.EMBEDDING: TargetDefinition(
        target=InferenceTarget.EMBEDDING,
        env_token="EMBEDDING",
        required_capabilities=frozenset(
            {
                InferenceCapability.EMBED,
                InferenceCapability.ESTIMATE_INPUT_TOKENS,
            }
        ),
        criticality=TargetCriticality.DEGRADABLE,
        failure_policy=TargetFailurePolicy.INDEX_RETRY,
        requires_output_limit=False,
    ),
    InferenceTarget.HEAVY_REASONING: TargetDefinition(
        target=InferenceTarget.HEAVY_REASONING,
        env_token="HEAVY_REASONING",
        required_capabilities=frozenset(
            {
                InferenceCapability.GENERATE_TEXT,
                InferenceCapability.ESTIMATE_INPUT_TOKENS,
            }
        ),
        criticality=TargetCriticality.OPTIONAL,
        failure_policy=TargetFailurePolicy.OPTIONAL_ERROR,
        requires_output_limit=True,
    ),
}


@dataclass(frozen=True)
class InferenceSettings:
    targets: Mapping[InferenceTarget, ResolvedTarget]

    def target(self, target: InferenceTarget) -> ResolvedTarget:
        try:
            return self.targets[target]
        except KeyError:
            raise ValueError(f"inference target is not configured: {target.value}") from None


def target_environment_key(
    target: InferenceTarget, suffix: str = ""
) -> str:
    if suffix not in _SUFFIXES:
        raise ValueError("unknown inference target environment suffix")
    return f"{INFERENCE_TARGET_PREFIX}{TARGET_DEFINITIONS[target].env_token}{suffix}"


def parse_provider_reference(value: str) -> ProviderReference:
    if not value or value.strip() != value or "/" not in value:
        raise ValueError("inference target must use canonical provider/model syntax")
    provider_id, model_id = value.split("/", 1)
    if not provider_id or not model_id or "@" in provider_id:
        raise ValueError("inference target provider/model syntax is invalid")
    if provider_id.strip() != provider_id or model_id.strip() != model_id:
        raise ValueError("inference target provider/model syntax is invalid")
    return ProviderReference(provider_id=provider_id, model_id=model_id)


def resolve_inference_settings(
    environment: Mapping[str, str],
    registry: ProviderRegistry,
) -> InferenceSettings:
    allowed_keys = {
        target_environment_key(target, suffix)
        for target in TARGET_DEFINITIONS
        for suffix in _SUFFIXES
    }
    unknown_keys = sorted(
        key
        for key in environment
        if key.startswith(INFERENCE_TARGET_PREFIX) and key not in allowed_keys
    )
    if unknown_keys:
        raise ValueError(f"unknown inference target setting: {unknown_keys[0]}")

    targets: dict[InferenceTarget, ResolvedTarget] = {}
    for target, definition in TARGET_DEFINITIONS.items():
        base_key = target_environment_key(target)
        raw_reference = environment.get(base_key)
        if raw_reference is None:
            if definition.criticality is TargetCriticality.OPTIONAL:
                continue
            raise ValueError(f"missing inference target setting: {base_key}")
        reference = parse_provider_reference(raw_reference)
        descriptor = registry.descriptor(reference.provider_id)
        if definition.local_only and descriptor.kind is not ProviderKind.LOCAL:
            raise ValueError(f"{base_key} requires a local provider")
        missing_capabilities = definition.required_capabilities - descriptor.capabilities
        if missing_capabilities:
            first = sorted(capability.value for capability in missing_capabilities)[0]
            raise ValueError(f"{base_key} provider lacks capability: {first}")
        options = _options(environment, target)
        descriptor.validate_options(options)
        max_input_tokens = _required_positive_integer(
            environment, target_environment_key(target, "_MAX_INPUT_TOKENS")
        )
        max_output_tokens = (
            _required_positive_integer(
                environment, target_environment_key(target, "_MAX_OUTPUT_TOKENS")
            )
            if definition.requires_output_limit
            else _optional_positive_integer(
                environment, target_environment_key(target, "_MAX_OUTPUT_TOKENS")
            )
        )
        timeout_seconds = _optional_positive_decimal(
            environment,
            target_environment_key(target, "_TIMEOUT_SECONDS"),
            DEFAULT_TIMEOUT_SECONDS,
        )
        max_concurrency = _optional_positive_integer(
            environment,
            target_environment_key(target, "_MAX_CONCURRENCY"),
            DEFAULT_MAX_CONCURRENCY,
        )
        if max_concurrency is None:
            raise AssertionError("default max concurrency must be configured")
        targets[target] = ResolvedTarget(
            definition=definition,
            reference=reference,
            options=MappingProxyType(dict(options)),
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
        )
    return InferenceSettings(targets=MappingProxyType(targets))


def _options(
    environment: Mapping[str, str], target: InferenceTarget
) -> Mapping[str, JsonValue]:
    key = target_environment_key(target, "_OPTIONS_JSON")
    raw_value = environment.get(key)
    if raw_value is None:
        return {}
    try:
        value: object = json.loads(raw_value)
    except json.JSONDecodeError:
        raise ValueError(f"{key} must be valid JSON") from None
    if not isinstance(value, dict) or any(not isinstance(name, str) for name in value):
        raise ValueError(f"{key} must be a JSON object")
    return cast(dict[str, JsonValue], value)


def _required_positive_integer(environment: Mapping[str, str], key: str) -> int:
    if key not in environment:
        raise ValueError(f"missing inference target setting: {key}")
    return _positive_integer(environment[key], key)


def _optional_positive_integer(
    environment: Mapping[str, str], key: str, default: int | None = None
) -> int | None:
    raw_value = environment.get(key)
    return default if raw_value is None else _positive_integer(raw_value, key)


def _positive_integer(raw_value: str, key: str) -> int:
    if not raw_value.isascii() or not raw_value.isdecimal():
        raise ValueError(f"{key} must be a positive integer")
    value = int(raw_value)
    if value < 1 or str(value) != raw_value:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _optional_positive_decimal(
    environment: Mapping[str, str], key: str, default: float
) -> float:
    raw_value = environment.get(key)
    if raw_value is None:
        return default
    if _DECIMAL_PATTERN.fullmatch(raw_value) is None:
        raise ValueError(f"{key} must be a positive decimal")
    try:
        value = Decimal(raw_value)
    except InvalidOperation:
        raise ValueError(f"{key} must be a positive decimal") from None
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{key} must be a positive decimal")
    return float(value)
