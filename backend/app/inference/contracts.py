from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from threading import Event
from typing import Protocol, TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class InferenceTarget(str, Enum):
    CHAT = "chat"
    PRIVACY = "privacy"
    MEMORY_EXTRACTION = "memory-extraction"
    MEMORY_CONSOLIDATION = "memory-consolidation"
    EMBEDDING = "embedding"
    VISION = "vision"
    HEAVY_REASONING = "heavy-reasoning"


class InferenceCapability(str, Enum):
    GENERATE_TEXT = "generate_text"
    STREAM_TEXT = "stream_text"
    GENERATE_STRUCTURED = "generate_structured"
    IMAGE_INPUT = "image_input"
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
class InferenceTextPart:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("inference text part must not be empty")


@dataclass(frozen=True)
class InferenceImagePart:
    """Coreがdecode検証する生画像。reprへ本文を含めない。"""

    data: bytes = field(repr=False)
    mime_type: str
    width: int
    height: int

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("inference image data must be bytes")
        if not isinstance(self.mime_type, str):
            raise TypeError("inference image MIME type must be a string")
        for name, value in (("width", self.width), ("height", self.height)):
            if type(value) is not int or value < 1:
                raise ValueError(f"inference image {name} must be positive")


InferenceContentPart: TypeAlias = InferenceTextPart | InferenceImagePart
InferenceContent: TypeAlias = str | tuple[InferenceContentPart, ...]


@dataclass(frozen=True)
class InferenceMessage:
    role: str
    content: InferenceContent

    def __post_init__(self) -> None:
        if self.role not in {"system", "developer", "user", "assistant"}:
            raise ValueError("inference message role is invalid")
        if isinstance(self.content, str):
            return
        if not isinstance(self.content, tuple):
            raise TypeError("inference message content must be text or typed parts")
        if not self.content:
            raise ValueError("inference message parts must not be empty")
        if any(
            not isinstance(part, (InferenceTextPart, InferenceImagePart))
            for part in self.content
        ):
            raise TypeError("inference message contains an unsupported part")


@dataclass(frozen=True)
class ModelProbeResult:
    """取得可能なmodel metadata上のCapability。Noneは未確認を表す。"""

    capabilities: frozenset[InferenceCapability] | None = None


class InferenceCancellationToken:
    """外部処理を停止できなくても、完了結果の採用を禁止する。"""

    def __init__(self) -> None:
        self._cancelled = Event()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()


@dataclass(frozen=True)
class ProviderReference:
    provider_id: str
    model_id: str


@dataclass(frozen=True)
class ImageInputLimits:
    allowed_mime_types: frozenset[str]
    max_images: int
    max_bytes: int
    max_width: int
    max_height: int
    max_pixels: int

    def __post_init__(self) -> None:
        if not self.allowed_mime_types or any(
            not isinstance(value, str) or not value
            for value in self.allowed_mime_types
        ):
            raise ValueError("image input MIME types must not be empty")
        for name, value in (
            ("max_images", self.max_images),
            ("max_bytes", self.max_bytes),
            ("max_width", self.max_width),
            ("max_height", self.max_height),
            ("max_pixels", self.max_pixels),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"image input {name} must be positive")


@dataclass(frozen=True)
class TargetDefinition:
    target: InferenceTarget
    env_token: str
    required_capabilities: frozenset[InferenceCapability]
    criticality: TargetCriticality
    failure_policy: TargetFailurePolicy
    requires_output_limit: bool
    local_only: bool = False
    image_limits: ImageInputLimits | None = None

    def __post_init__(self) -> None:
        requires_image = InferenceCapability.IMAGE_INPUT in self.required_capabilities
        if requires_image != (self.image_limits is not None):
            raise ValueError("image capability and limits must be declared together")


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

    def probe(
        self, model_id: str, *, timeout_seconds: float
    ) -> ModelProbeResult: ...
