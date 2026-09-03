from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from app.inference.contracts import (
    InferenceAdapter,
    InferenceCapability,
    JsonValue,
    ProviderKind,
)


OptionValidator = Callable[[Mapping[str, JsonValue]], None]


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    kind: ProviderKind
    capabilities: frozenset[InferenceCapability]
    validate_options: OptionValidator

    def __post_init__(self) -> None:
        if not self.provider_id or self.provider_id.strip() != self.provider_id:
            raise ValueError("provider id must be canonical")
        if "/" in self.provider_id or "@" in self.provider_id:
            raise ValueError("provider id contains a reserved character")
        if not self.capabilities:
            raise ValueError("provider must declare at least one capability")


class ProviderRegistry:
    def __init__(self, descriptors: Iterable[ProviderDescriptor]) -> None:
        self._descriptors: dict[str, ProviderDescriptor] = {}
        self._adapters: dict[str, InferenceAdapter] = {}
        for descriptor in descriptors:
            if descriptor.provider_id in self._descriptors:
                raise ValueError(f"duplicate provider: {descriptor.provider_id}")
            self._descriptors[descriptor.provider_id] = descriptor

    @property
    def descriptors(self) -> Mapping[str, ProviderDescriptor]:
        return self._descriptors

    def descriptor(self, provider_id: str) -> ProviderDescriptor:
        try:
            return self._descriptors[provider_id]
        except KeyError:
            raise ValueError(f"unknown inference provider: {provider_id}") from None

    def bind(self, adapter: InferenceAdapter) -> None:
        descriptor = self.descriptor(adapter.provider_id)
        if adapter.capabilities != descriptor.capabilities:
            raise ValueError(
                f"adapter capability declaration differs for {adapter.provider_id}"
            )
        self._adapters[adapter.provider_id] = adapter

    def adapter(self, provider_id: str) -> InferenceAdapter:
        self.descriptor(provider_id)
        try:
            return self._adapters[provider_id]
        except KeyError:
            raise RuntimeError(
                f"inference adapter is not bound: {provider_id}"
            ) from None


def _reject_secret_options(options: Mapping[str, JsonValue]) -> None:
    forbidden_fragments = (
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "authorization",
        "credential",
    )
    for key in options:
        normalized = key.lower().replace("-", "_")
        if any(fragment in normalized for fragment in forbidden_fragments):
            raise ValueError("inference options must not contain credentials")


def _validate_known_options(
    options: Mapping[str, JsonValue],
    *,
    allowed: frozenset[str],
) -> None:
    _reject_secret_options(options)
    unknown = set(options) - allowed
    if unknown:
        raise ValueError(f"unsupported inference option: {sorted(unknown)[0]}")
    if "reasoning_effort" in options and options["reasoning_effort"] not in {
        "low",
        "medium",
        "high",
    }:
        raise ValueError("reasoning_effort must be low, medium, or high")
    for key in {"temperature", "top_p"} & set(options):
        value = options[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be numeric")
        if key == "temperature" and not 0 <= value <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if key == "top_p" and not 0 <= value <= 1:
            raise ValueError("top_p must be between 0 and 1")
    if "seed" in options and type(options["seed"]) is not int:
        raise ValueError("seed must be an integer")


def default_provider_registry() -> ProviderRegistry:
    all_capabilities = frozenset(InferenceCapability)
    return ProviderRegistry(
        (
            ProviderDescriptor(
                provider_id="ollama",
                kind=ProviderKind.LOCAL,
                capabilities=all_capabilities,
                validate_options=lambda options: _validate_known_options(
                    options,
                    allowed=frozenset({"temperature", "top_p", "seed"}),
                ),
            ),
            ProviderDescriptor(
                provider_id="openai-api",
                kind=ProviderKind.CLOUD,
                capabilities=all_capabilities,
                validate_options=lambda options: _validate_known_options(
                    options,
                    allowed=frozenset(
                        {"temperature", "top_p", "seed", "reasoning_effort"}
                    ),
                ),
            ),
            ProviderDescriptor(
                provider_id="openai-codex",
                kind=ProviderKind.CLOUD,
                capabilities=frozenset(
                    {
                        InferenceCapability.GENERATE_TEXT,
                        InferenceCapability.ESTIMATE_INPUT_TOKENS,
                    }
                ),
                validate_options=lambda options: _validate_known_options(
                    options,
                    allowed=frozenset({"reasoning_effort"}),
                ),
            ),
        )
    )
