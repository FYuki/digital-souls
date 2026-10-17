"""Explicit no-auth loopback embeddings through the already pinned official SDK."""

import asyncio
import json
import math

import httpx
import openai
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .application import CoreError
from .character import managed_loopback_endpoint
from .local_sdk import local_openai_client
from .memory_ranking import EmbeddingSpace, validate_embedding_space
from .privacy_scan import scan


class LocalEmbeddingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    profile_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    model: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:/-]+$")
    model_digest: str = Field(min_length=1, max_length=128, repr=False)
    dimensions: int = Field(ge=1, le=4096)
    api_base: str
    timeout_seconds: float = Field(default=15, gt=0, le=15, allow_inf_nan=False)
    enabled: bool = False

    @model_validator(mode="after")
    def valid_local_profile(self) -> "LocalEmbeddingProfile":
        managed_loopback_endpoint(self.api_base)
        try:
            EmbeddingSpace(self.model, self.model_digest, self.dimensions)
        except CoreError:
            raise ValueError("invalid embedding identity") from None
        return self


class LocalEmbedding:
    """MemoryRetrieval supplies content authorization; this adapter owns local I/O only."""

    def __init__(self, profile: LocalEmbeddingProfile) -> None:
        # Revalidate even a profile created through model_copy/model_construct.
        self._profile = LocalEmbeddingProfile.model_validate(profile.model_dump())

    @property
    def space(self) -> EmbeddingSpace:
        profile = LocalEmbeddingProfile.model_validate(self._profile.model_dump())
        return EmbeddingSpace(
            profile.model,
            profile.model_digest,
            profile.dimensions,
            configuration=json.dumps(
                {
                    "adapter": "local-openai-embeddings-v1",
                    "sdk": openai.__version__,
                    "profile_id": profile.profile_id,
                    "endpoint": managed_loopback_endpoint(profile.api_base),
                    "enabled": profile.enabled,
                    "timeout_seconds": profile.timeout_seconds,
                },
                sort_keys=True,
            ),
        )

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        try:
            space = validate_embedding_space(self.space)
            profile = LocalEmbeddingProfile.model_validate(self._profile.model_dump())
            if type(texts) is not tuple or len(texts) > 1001:
                raise ValueError
            if not texts:
                return ()
            if (
                not profile.enabled
                or any(type(text) is not str or not text.strip() for text in texts)
                or sum(len(text.encode("utf-8")) for text in texts) > 263168
            ):
                raise ValueError
            finding = scan(texts)
            if finding.secret or finding.failed:
                raise ValueError
            # Retire this deadline before transport cleanup. An outer service
            # deadline must not race a still-armed inner timer while closing I/O.
            async with local_openai_client(profile.api_base, profile.timeout_seconds) as client:
                async with asyncio.timeout(profile.timeout_seconds):
                    response = await client.embeddings.with_raw_response.create(
                        model=profile.model, input=list(texts), encoding_format="float"
                    )
            if space != self.space:
                raise ValueError
            # SDK typed response construction can coerce booleans into
            # floats. The official raw-response wrapper preserves JSON types for
            # our strict numeric boundary without implementing another transport.
            data = response.http_response.json()
            if data.get("object") != "list" or data.get("model") != profile.model:
                raise ValueError
            rows = data.get("data")
            if type(rows) is not list or len(rows) != len(texts):
                raise ValueError
            indexed: dict[int, tuple[float, ...]] = {}
            for row in rows:
                if type(row) is not dict or row.get("object") != "embedding":
                    raise ValueError
                index, vector = row.get("index"), row.get("embedding")
                if (
                    type(index) is not int
                    or not 0 <= index < len(texts)
                    or index in indexed
                    or type(vector) is not list
                    or len(vector) != space.dimensions
                    or any(type(number) not in (int, float) for number in vector)
                ):
                    raise ValueError
                values = tuple(float(number) for number in vector)
                if not all(math.isfinite(number) for number in values) or not any(values):
                    raise ValueError
                indexed[index] = values
            return tuple(indexed[index] for index in range(len(texts)))
        except (TimeoutError, httpx.TimeoutException, openai.APITimeoutError):
            raise CoreError(504, "memory_embedding_timeout", "Local embedding timed out") from None
        except Exception:
            raise CoreError(502, "memory_embedding_failed", "Local embedding failed") from None
