"""One inference path shared by both HTTP entry points."""

from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Protocol

from .character import (
    AccessScope,
    Character,
    ContextSource,
    EmptyContext,
    GuardedContextSource,
    Profile,
)
from .character_book import select_lore
from .contracts import CompletionInput
from .errors import CoreError as CoreError
from .history import Binding
from .privacy import PrivacyPolicy, destination
from .prompt_builder import (
    ByteTokenCounter,
    MemoryItem,
    PromptBudgetExceeded,
    PromptBuilder,
    PromptInput,
    PromptUsage,
    split_messages,
)
from .token_count import LlamaCppTokenCounter


class Provider(Protocol):
    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]: ...

    def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]: ...


@dataclass(frozen=True)
class Prepared:
    character: Character
    payload: dict[str, Any] = field(repr=False)
    usage: PromptUsage
    valid: Callable[[], bool] = lambda: True

    @property
    def headers(self) -> dict[str, str]:
        config = self.character.config
        return {
            "X-Character-ID": config.character_id,
            "X-Character-Config-Version": config.config_version,
            "X-Character-Card-SHA256": self.character.card_sha256,
            "X-Inference-Profile": config.profile.profile_id,
        }


class Inference:
    """Stateless character selection and context assembly shared by HTTP entry points."""

    def __init__(
        self,
        characters: tuple[Character, ...],
        provider: Provider,
        context: ContextSource | None = None,
        *,
        privacy: PrivacyPolicy | None = None,
        memory_context: GuardedContextSource | None = None,
    ) -> None:
        by_id = {char.config.character_id: char for char in characters}
        aliases = {char.config.alias: char for char in characters}
        if len(by_id) != len(characters) or len(aliases) != len(characters):
            raise ValueError("duplicate character id or alias")
        self.characters: Mapping[str, Character] = MappingProxyType(by_id)
        self.aliases: Mapping[str, Character] = MappingProxyType(aliases)
        self.provider = provider
        self.context = context or EmptyContext()
        self.scope = AccessScope()
        self.privacy = privacy
        self.memory_context = memory_context

    async def prepare(
        self,
        selector: str,
        request: CompletionInput,
        *,
        alias: bool,
        conversation_id: str | None = None,
    ) -> Prepared:
        """Pin the character, authorize export/capabilities, and bound injected context.

        Context lookup receives server-trusted scope only after export is allowed.
        A fresh payload keeps concurrent characters and caller histories isolated.
        """
        if any(message.role == "system" for message in request.messages):
            raise CoreError(
                400, "system_message_unsupported", "Caller system messages are unsupported"
            )
        character = (self.aliases if alias else self.characters).get(selector)
        if character is None:
            raise CoreError(404, "character_not_found", "Unknown character or model alias")
        profile = character.config.profile
        if not profile.external_send_allowed:
            raise CoreError(403, "external_send_denied", "Character export policy denies inference")
        payload = request.model_dump(exclude_none=True, exclude={"model", "character_id"})
        if "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        required = set(payload) - {"messages"}
        if not request.stream:
            required.discard("stream")
        if any(message.tool_calls or message.role == "tool" for message in request.messages):
            required.add("tools")
        if required - profile.allowed_parameters:
            raise CoreError(400, "unsupported_parameter", "Parameter capability is not confirmed")
        binding = Binding(self.scope, character.config.character_id)
        privacy = self.privacy
        if privacy is not None and not await privacy.authorize(
            binding, destination(profile), payload
        ):
            raise CoreError(403, "privacy_denied", "Privacy policy denies inference")
        if privacy is not self.privacy:
            raise CoreError(403, "privacy_denied", "Privacy policy changed during inference")
        user_text = next(
            (msg.content or "" for msg in reversed(request.messages) if msg.role == "user"), ""
        )
        stamp = privacy.stamp if privacy is not None else None
        extra = await self.context.context(character, binding.scope, user_text)
        memory_context = self.memory_context

        def authorized() -> bool:
            return (
                self.privacy is privacy
                and self.scope == binding.scope
                and (privacy is None or privacy.stamp == stamp)
                and self.memory_context is memory_context
            )

        if not authorized():
            raise CoreError(403, "privacy_denied", "Context policy changed")
        guarded = None
        if conversation_id is not None and memory_context is not None:
            if privacy is None or memory_context.policy is not privacy:
                raise CoreError(403, "privacy_denied", "Memory and inference policy differ")
            guarded = await memory_context.context(
                character, binding.scope, user_text, authorized=authorized
            )
            if guarded.policy is not privacy:
                raise CoreError(403, "privacy_denied", "Memory and inference policy differ")
        separated = split_messages(request.messages)
        # ContextSource is the legacy trusted memory/skill instruction extension,
        # not a personality/relationship/life-state snapshot (ADR 0020).
        card = character.card
        if extra:
            card = replace(
                card,
                system_prompt="\n\n".join(part for part in (card.system_prompt, extra) if part),
            )
        memory_items = () if guarded is None else guarded.items
        if guarded is not None and guarded.text and not memory_items:
            # Existing custom guarded sources remain one indivisible item.
            memory_items = (guarded.text,)

        def valid() -> bool:
            return (
                self.scope == binding.scope
                and self.privacy is privacy
                and (privacy is None or privacy.stamp == stamp)
                and (guarded is None or (self.memory_context is memory_context and guarded.valid()))
            )

        async def authorize_count(candidate: dict[str, Any]) -> None:
            if not valid():
                raise CoreError(403, "context_revoked", "Context authorization changed")
            if privacy is not None and not await privacy.authorize(
                binding, destination(profile), candidate
            ):
                raise CoreError(403, "privacy_denied", "Privacy policy denies token counting")
            if not valid():
                raise CoreError(403, "context_revoked", "Context authorization changed")

        config = character.config
        try:
            counter = (
                LlamaCppTokenCounter(
                    profile.api_base or "",
                    profile.timeout_seconds,
                    tool_choice=payload.get("tool_choice"),
                    before_send=authorize_count,
                )
                if profile.token_counter == "llamacpp"
                else ByteTokenCounter()
            )
            selection = await select_lore(
                character.character_book,
                user_text,
                tuple(message for turn in separated.history for message in turn.messages),
                counter,
            )
            result = await PromptBuilder(
                counter,
                memory_floor=config.memory_floor,
                history_floor=config.history_floor,
            ).build(
                PromptInput(
                    card=card,
                    lore=selection.entries,
                    memory=tuple(
                        MemoryItem(item, json_object=bool(guarded and guarded.items))
                        for item in memory_items
                    ),
                    history=separated.history,
                    current_input=separated.current_input,
                    tools=tuple(request.tools or ()),
                ),
                config.prompt_budget.budgets(),
            )
        except PromptBudgetExceeded as error:
            raise CoreError(400, "prompt_budget_exceeded", error.region) from None
        payload["messages"] = [message.model_dump(exclude_none=True) for message in result.messages]
        if privacy is not self.privacy or (
            privacy is not None
            and not await privacy.authorize(binding, destination(profile), payload)
        ):
            raise CoreError(403, "privacy_denied", "Privacy policy denies inference")
        if privacy is not self.privacy:
            raise CoreError(403, "privacy_denied", "Privacy policy changed during inference")

        usage = replace(
            result.usage,
            dropped=MappingProxyType(
                {
                    **result.usage.dropped,
                    "lore": result.usage.dropped["lore"]
                    + sum(
                        item.reason == "omitted_by_lore_budget" for item in selection.diagnostics
                    ),
                }
            ),
        )
        prepared = Prepared(character, payload, usage, valid)
        self.check(prepared)
        return prepared

    @staticmethod
    def check(prepared: Prepared) -> None:
        if not prepared.valid():
            raise CoreError(403, "context_revoked", "Context authorization changed")
