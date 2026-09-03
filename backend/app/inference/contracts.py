from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class InferenceTarget(str, Enum):
    CHAT = "chat"
    PRIVACY = "privacy"
    MEMORY_EXTRACTION = "memory-extraction"
    MEMORY_CONSOLIDATION = "memory-consolidation"
    EMBEDDING = "embedding"
    HEAVY_REASONING = "heavy-reasoning"


class InferenceCapability(str, Enum):
    GENERATE_TEXT = "generate_text"
    STREAM_TEXT = "stream_text"
    GENERATE_STRUCTURED = "generate_structured"
    EMBED = "embed"
    ESTIMATE_INPUT_TOKENS = "estimate_input_tokens"


class ProviderKind(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


class TargetCriticality(str, Enum):
    REQUIRED = "required"
    DEGRADABLE = "degradable"
    OPTIONAL = "optional"


class TargetFailurePolicy(str, Enum):
    CHAT_ERROR = "chat_error"
    PRIVACY_ABSTAIN = "privacy_abstain"
    WORKER_RETRY = "worker_retry"
    NOOP = "noop"
    INDEX_RETRY = "index_retry"
    OPTIONAL_ERROR = "optional_error"


class TokenEstimateAccuracy(str, Enum):
    EXACT = "exact"
    ESTIMATED = "estimated"


@dataclass(frozen=True)
class InferenceMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "developer", "user", "assistant"}:
            raise ValueError("inference message role is invalid")
        if not isinstance(self.content, str):
            raise TypeError("inference message content must be a string")


@dataclass(frozen=True)
class ProviderReference:
    provider_id: str
    model_id: str


@dataclass(frozen=True)
class TargetDefinition:
    target: InferenceTarget
    env_token: str
    required_capabilities: frozenset[InferenceCapability]
    criticality: TargetCriticality
    failure_policy: TargetFailurePolicy
    requires_output_limit: bool
    local_only: bool = False


@dataclass(frozen=True)
class ResolvedTarget:
    definition: TargetDefinition
    reference: ProviderReference
    options: Mapping[str, JsonValue]
    max_input_tokens: int
    max_output_tokens: int | None
    timeout_seconds: float
    max_concurrency: int


@dataclass(frozen=True)
class TokenEstimate:
    count: int
    accuracy: TokenEstimateAccuracy
    method: str

    def __post_init__(self) -> None:
        if type(self.count) is not int or self.count < 0:
            raise ValueError("token estimate count must be a non-negative integer")
        if not self.method.strip():
            raise ValueError("token estimate method must not be blank")


@dataclass(frozen=True)
class InferenceUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    provider_reported: bool

    def __post_init__(self) -> None:
        counts = (self.input_tokens, self.output_tokens, self.total_tokens)
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("inference usage values must be non-negative integers")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("inference usage total must equal input plus output")


@dataclass(frozen=True)
class TextGenerationRequest:
    messages: tuple[InferenceMessage, ...]
    model_id: str
    options: Mapping[str, JsonValue]
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class StructuredGenerationRequest(TextGenerationRequest):
    response_schema: Mapping[str, object]


@dataclass(frozen=True)
class EmbeddingRequest:
    inputs: tuple[str, ...]
    model_id: str
    options: Mapping[str, JsonValue]
    max_input_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class TokenEstimateRequest:
    messages: tuple[InferenceMessage, ...]
    model_id: str
    options: Mapping[str, JsonValue]
    max_input_tokens: int
    timeout_seconds: float
    response_schema: Mapping[str, object] | None = None


@dataclass(frozen=True)
class ProviderTextResult:
    text: str
    usage: InferenceUsage | None = None


@dataclass(frozen=True)
class TextGenerationResult:
    text: str
    usage: InferenceUsage | None


@dataclass(frozen=True)
class StructuredGenerationResult:
    value: JsonValue
    usage: InferenceUsage | None


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    usage: InferenceUsage | None = None


class InferenceAdapter(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[InferenceCapability]: ...

    def generate_text(self, request: TextGenerationRequest) -> ProviderTextResult: ...

    def stream_text(self, request: TextGenerationRequest) -> AsyncIterator[str]: ...

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> ProviderTextResult: ...

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult: ...

    def estimate_input_tokens(self, request: TokenEstimateRequest) -> TokenEstimate: ...
