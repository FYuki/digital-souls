"""Immutable runtime snapshots and the small bundled-card context importer."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .character_book import CharacterBook, load_book
from .prompt_builder import CharacterCard, TokenBudgets

type SupportedParameter = Literal[
    "stream", "tools", "tool_choice", "temperature", "max_completion_tokens"
]


def managed_loopback_endpoint(value: str) -> str:
    """Normalize the fixed no-auth local SDK destination without resolving hostnames."""
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("managed local endpoint required")
    endpoint = urlsplit(value)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname != "127.0.0.1"
        or endpoint.port is None
        or endpoint.port == 0
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.path != "/v1"
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("managed local endpoint requires a loopback HTTP port and /v1 path")
    return f"http://127.0.0.1:{endpoint.port}/v1"


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    model: str = Field(min_length=1, pattern=r"^[a-z0-9_]+/[^\s]+$")
    allowed_parameters: frozenset[SupportedParameter] = frozenset()
    external_send_allowed: StrictBool = False
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    ollama_think: StrictBool | None = None
    transport: Literal["sdk", "llamacpp_chat"] = "sdk"
    api_base: str | None = None
    token_counter: Literal["estimate", "llamacpp"] = "estimate"

    @model_validator(mode="after")
    def validate_provider_settings(self) -> "Profile":
        """Pin route-specific operator options and reject unverified local endpoints."""
        if self.token_counter == "llamacpp" and self.transport != "llamacpp_chat":
            raise ValueError("llamacpp token_counter requires llamacpp_chat transport")
        if "ollama_think" in self.model_fields_set and not self.model.startswith("ollama_chat/"):
            raise ValueError("ollama_think requires an ollama_chat model")
        if self.transport == "llamacpp_chat":
            if self.model != "openai/gemma4-12b" or self.api_base is None:
                raise ValueError(
                    "llamacpp_chat requires the verified gemma4-12b alias and api_base"
                )
            managed_loopback_endpoint(self.api_base)
        elif "api_base" in self.model_fields_set:
            raise ValueError("api_base is supported only for llamacpp_chat")
        return self


def local_destination_identity(profile: Profile) -> dict[str, str]:
    """Whitelist non-secret identity from the validated managed loopback profile."""
    checked = Profile.model_validate(profile.model_dump(exclude_none=True))
    if checked.transport != "llamacpp_chat" or checked.api_base is None:
        raise ValueError("managed local destination required")
    return {
        "destination_version": "managed-loopback-v1",
        "transport": checked.transport,
        "profile_id": checked.profile_id,
        "endpoint": managed_loopback_endpoint(checked.api_base),
    }


type PositiveInt = Annotated[int, Field(strict=True, gt=0)]


class PromptBudget(BaseModel):
    """Conservative defaults for a 32768 context with 4096 reserved output tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    total: PositiveInt = 28672
    character: PositiveInt = 16384
    lore: PositiveInt = 8192
    memory: PositiveInt = 16384
    history: PositiveInt = 24576
    current_input: PositiveInt = 16384
    post_history: PositiveInt = 4096

    def budgets(self) -> TokenBudgets:
        return TokenBudgets(**self.model_dump())


class CharacterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    character_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    config_version: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    alias: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    card_path: str
    profile: Profile
    prompt_budget: PromptBudget = Field(default_factory=PromptBudget)
    memory_floor: PositiveInt = 3
    history_floor: PositiveInt = 3


@dataclass(frozen=True)
class Character:
    config: CharacterConfig
    card: CharacterCard
    card_sha256: str = field(kw_only=True)
    character_book: CharacterBook | None = field(default=None, kw_only=True, repr=False)


@dataclass(frozen=True)
class AccessScope:
    """Server-trusted identity/use scope; never accepted from completion JSON.

    Only a single local operator is supported now. A future authentication
    boundary must establish subject/client/audience before any memory lookup.
    """

    subject: str = "local-operator"
    client: str = "local"
    audience: Literal["local-private"] = "local-private"


class ContextSource(Protocol):
    """Future memory/skill adapter: keyed by the pinned character and config version.

    An implementation must authorize retrieval/export before returning text.
    The initial implementation has no persistence or learning.
    """

    async def context(self, character: Character, scope: AccessScope, user_text: str) -> str: ...


@dataclass(frozen=True)
class GuardedContext:
    """Full JSON array plus ranked JSON objects; one guard covers the entire lookup."""

    text: str = field(repr=False)
    valid: Callable[[], bool] = field(repr=False)
    policy: object = field(repr=False)
    items: tuple[str, ...] = field(default=(), repr=False)


class GuardedContextSource(Protocol):
    @property
    def policy(self) -> object: ...

    async def context(
        self,
        character: Character,
        scope: AccessScope,
        user_text: str,
        *,
        authorized: Callable[[], bool],
    ) -> GuardedContext: ...


class EmptyContext:
    async def context(self, character: Character, scope: AccessScope, user_text: str) -> str:
        return ""


def load_characters(path: Path) -> tuple[Character, ...]:
    """Load operator configuration once at startup, not per inference request.

    Import prompt fields and validate the entire embedded Character Book.
    """
    configs = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(configs, list) or not configs:
        raise ValueError("configuration must be a nonempty character list")
    characters = []
    for value in configs:
        config = CharacterConfig.model_validate(value)
        card_bytes = (path.parent / config.card_path).read_bytes()
        try:
            card = json.loads(card_bytes.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ValueError("invalid card encoding or JSON") from None
        card_sha256 = hashlib.sha256(card_bytes).hexdigest()
        if card.get("spec") != "chara_card_v3" or card.get("spec_version") != "3.0":
            raise ValueError("unsupported card version")
        data = card["data"]
        fields = (
            "description",
            "personality",
            "scenario",
            "system_prompt",
            "mes_example",
            "post_history_instructions",
        )
        if any(not isinstance(data.get(key, ""), str) for key in fields):
            raise ValueError("card prompt fields must be text")
        prompt_card = CharacterCard(**{key: data.get(key, "") for key in fields})
        if not any(getattr(prompt_card, key).strip() for key in fields[:-1]):
            raise ValueError("empty prompt")
        book = load_book(data["character_book"]) if "character_book" in data else None
        characters.append(
            Character(config, prompt_card, card_sha256=card_sha256, character_book=book)
        )
    return tuple(characters)
