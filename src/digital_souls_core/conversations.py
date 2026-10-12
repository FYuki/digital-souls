"""Opt-in stateful path; the existing Inference API remains stateless."""

import asyncio
import hashlib
import json
from contextlib import aclosing
from io import StringIO
from typing import Any

from pydantic import ValidationError

from .application import CoreError, Inference
from .character import CharacterConfig, Profile
from .contracts import CompletionInput, Message
from .history import (
    Binding,
    ConversationControls,
    HistoryPolicy,
    HistoryStore,
    MemoryConfirmation,
    Operation,
    Receipt,
    Snapshot,
    TurnDeletionInput,
    TurnDeletionResult,
    TurnInput,
)
from .privacy import PrivacyPolicy
from .privacy_scan import scan

MAX_BYTES = 1024 * 1024


def bounded(value: object) -> None:
    """Reject a JSON value exceeding the UTF-8 history byte budget."""
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        raise CoreError(413, "history_limit", "Conversation byte limit exceeded")


def request_fingerprint(body: TurnInput, config: CharacterConfig) -> str:
    """Hash input and routing configuration for idempotent receipt lookup."""
    profile = config.profile.model_dump(mode="json")
    if config.profile.transport == "sdk":
        # Preserve pre-llamacpp receipts: newly added defaults do not change routing.
        profile.pop("transport")
        profile.pop("api_base")
    if config.profile.token_counter == "estimate":
        # The existing estimate is unchanged; only a new counting mode changes identity.
        profile.pop("token_counter")
    profile["allowed_parameters"] = sorted(config.profile.allowed_parameters)
    request_body = body.model_dump()
    if not body.memory_excluded_indices:
        request_body.pop("memory_excluded_indices")  # Preserve pre-control receipts.
    return hashlib.sha256(
        json.dumps(
            [request_body, config.config_version, profile],
            sort_keys=True,
        ).encode()
    ).hexdigest()


class Conversations:
    """Coordinate scoped history, policy checks and one inference turn."""

    def __init__(
        self, inference: Inference, store: HistoryStore, policy: HistoryPolicy | None = None
    ) -> None:
        if isinstance(policy, PrivacyPolicy) and inference.privacy is not policy:
            raise ValueError("history and inference must share the same privacy policy")
        self.inference = inference
        self.store = store
        self.policy = policy

    def binding(self, character_id: str) -> Binding:
        """Bind a known character to the trusted server-side access scope."""
        if character_id not in self.inference.characters:
            raise CoreError(404, "character_not_found", "Unknown character")
        return Binding(self.inference.scope, character_id)

    def authorize(
        self, operation: Operation, binding: Binding, messages: tuple[Message, ...] = ()
    ) -> None:
        """Fail closed unless the injected policy explicitly allows the operation."""
        try:
            allowed = (
                self.policy is not None
                and (
                    not isinstance(self.policy, PrivacyPolicy)
                    or self.inference.privacy is self.policy
                )
                and self.policy.allows(
                    operation, binding, tuple(m.model_copy(deep=True) for m in messages)
                )
                is True
            )
        except Exception:
            allowed = False
        if not allowed:
            raise CoreError(403, "history_policy_denied", "History policy denies operation")

    def create(self, character_id: str) -> Snapshot:
        """Create an empty conversation after explicit policy authorization."""
        binding = self.binding(character_id)
        self.authorize("create", binding)
        return self.store.create(binding)

    def list(self, character_id: str, *, include_archived: bool = False) -> list[str]:
        """List conversation IDs within the authorized character and caller scope."""
        binding = self.binding(character_id)
        self.authorize("list", binding)
        return self.store.list(binding, include_archived=include_archived)

    def read(self, character_id: str, conversation_id: str) -> Snapshot:
        """Restore a scoped snapshot and reauthorize its actual content."""
        binding = self.binding(character_id)
        self.authorize("read", binding)
        snapshot = self.store.read(binding, conversation_id)
        self.authorize("read", binding, snapshot.messages)
        return snapshot

    def delete(self, character_id: str, conversation_id: str) -> None:
        """Delete scoped history even when storage consent has been revoked."""
        self.store.delete(self.binding(character_id), conversation_id)

    def delete_turns(
        self, character_id: str, conversation_id: str, selection: TurnDeletionInput
    ) -> TurnDeletionResult:
        return self.store.delete_turns(self.binding(character_id), conversation_id, selection)

    def controls(
        self, character_id: str, conversation_id: str, changes: ConversationControls
    ) -> Snapshot:
        """Apply explicit thread modes without inferring them from conversation text."""
        binding = self.binding(character_id)
        self.authorize("store", binding)
        self.read(character_id, conversation_id)
        snapshot = self.store.controls(binding, conversation_id, changes)
        self.authorize("read", binding, snapshot.messages)
        return snapshot

    def confirm(
        self, character_id: str, conversation_id: str, answer: MemoryConfirmation
    ) -> Snapshot:
        binding = self.binding(character_id)
        self.authorize("store", binding)
        self.read(character_id, conversation_id)
        snapshot = self.store.confirm(binding, conversation_id, answer)
        self.authorize("read", binding, snapshot.messages)
        return snapshot

    async def complete(self, character_id: str, conversation_id: str, body: TurnInput) -> Receipt:
        """Return an authorized retry receipt or atomically save a completed turn."""
        binding = self.binding(character_id)
        incoming = tuple(body.messages)
        if any(message.role == "system" for message in incoming):
            raise CoreError(
                400, "system_message_unsupported", "Caller system messages are unsupported"
            )
        if any(message.role not in ("user", "tool") for message in incoming):
            raise CoreError(400, "invalid_turn", "Only new user or tool messages are accepted")
        bounded(body.model_dump())
        self.authorize("store", binding, incoming)
        snapshot = self.read(character_id, conversation_id)
        config = self.inference.characters[character_id].config
        fingerprint = request_fingerprint(body, config)
        prior = self.store.receipt(binding, conversation_id, body.request_id)
        if prior is not None:
            if prior.fingerprint != fingerprint:
                raise CoreError(409, "request_conflict", "Request id has different input")
            self.authorize("read", binding, (prior.message,))
            return prior
        if snapshot.revision != body.expected_revision:
            raise CoreError(409, "revision_conflict", "Conversation revision changed")
        confirmation_indices = []
        for index, item in enumerate(incoming):
            if item.role == "user":
                finding = scan(item.content)
                if finding.failed:
                    raise CoreError(403, "history_policy_denied", "Refusal screening failed")
                if finding.no_history or finding.no_memory:
                    confirmation_indices.append(index)
        all_input = (*snapshot.messages, *incoming)
        payload = body.model_dump(
            exclude={"request_id", "expected_revision", "memory_excluded_indices"},
            exclude_none=True,
        )
        payload["messages"] = [m.model_dump(exclude_none=True) for m in all_input]
        bounded(payload)
        try:
            request = CompletionInput.model_validate(payload)
        except ValidationError:
            raise CoreError(400, "invalid_turn", "Invalid conversation or tool sequence") from None
        self.authorize("export", binding, all_input)
        prepared = await self.inference.prepare(
            character_id, request, alias=False, conversation_id=conversation_id
        )
        profile = prepared.character.config.profile
        self.authorize("export", binding, all_input)
        self.inference.check(prepared)
        try:
            if body.stream:
                message, finish = await self._stream(profile, prepared.payload)
            else:
                result = await self.inference.provider.complete(profile, prepared.payload)
                choices = result["choices"]
                if len(choices) != 1 or choices[0]["index"] != 0:
                    raise ValueError("one choice required")
                finish = choices[0]["finish_reason"]
                message = self._visible(choices[0]["message"])
            self._validate_result(request, message, finish)
        except CoreError:
            raise
        except Exception:
            raise CoreError(
                502, "invalid_history_result", "Provider result cannot be saved"
            ) from None
        # Let an already queued disconnect cancel this task before the synchronous
        # policy/commit section. No event-loop await occurs inside that section.
        await asyncio.sleep(0)
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        stored = (*incoming, message)
        bounded([m.model_dump() for m in (*snapshot.messages, *stored)])
        # Re-evaluate after await: consent can be revoked while inference is active.
        self.authorize("store", binding, (*snapshot.messages, *stored))
        self.authorize("read", binding, (message,))
        receipt = self.store.append(
            binding,
            conversation_id,
            body.request_id,
            fingerprint,
            snapshot.revision,
            stored,
            finish,
            card_sha256=prepared.character.card_sha256,
            **(
                {"memory_confirmation_indices": tuple(confirmation_indices)}
                if confirmation_indices
                else {}
            ),
            **(
                {"memory_excluded_indices": tuple(body.memory_excluded_indices)}
                if body.memory_excluded_indices
                else {}
            ),
        )
        # A concurrent identical request may have won with a different answer.
        # Authorize the actual receipt, never substitute or rewrite that winner.
        self.authorize("read", binding, (receipt.message,))
        return receipt

    @staticmethod
    def _visible(raw: dict[str, Any]) -> Message:
        # Allowlist only the public assistant message. Never persist SDK envelopes,
        # reasoning_content, thinking, usage, credentials, injected prompts or errors.
        message = Message.model_validate(
            {
                k: v
                for k, v in raw.items()
                if k in {"role", "content", "tool_calls"} and v is not None
            }
        )
        if message.role != "assistant":
            raise ValueError("assistant required")
        if message.content and any(
            marker in message.content.casefold() for marker in ("<think", "</think", "<analysis")
        ):
            raise ValueError("embedded reasoning is not history")
        return message

    @staticmethod
    def _validate_result(request: CompletionInput, message: Message, finish: str) -> None:
        if finish not in ("stop", "tool_calls") or (finish == "tool_calls") != bool(
            message.tool_calls
        ):
            raise ValueError("incomplete or inconsistent provider result")
        names = {tool.function.name for tool in request.tools or []}
        if any(call.function.name not in names for call in message.tool_calls or []):
            raise ValueError("unknown tool")
        # Input already has a validated, fully resolved tool sequence. Validate only
        # new IDs here; the response's tool results belong to a subsequent turn.
        used = {call.id for item in request.messages for call in item.tool_calls or []}
        for call in message.tool_calls or []:
            if call.id in used:
                raise ValueError("duplicate tool call id")
            used.add(call.id)

    async def _stream(self, profile: Profile, payload: dict[str, Any]) -> tuple[Message, str]:
        text = StringIO()
        calls: dict[int, dict[str, Any]] = {}
        budget = _StreamBudget()
        finish: str | None = None
        # Bounded buffering permits policy checks over complete tool arguments.
        # No partial result reaches disk or the conversation HTTP response.
        async with aclosing(self.inference.provider.stream(profile, payload)) as upstream:
            async for chunk in upstream:
                choices = chunk["choices"]
                if not choices:  # usage-only final chunk
                    continue
                if finish is not None or len(choices) != 1 or choices[0]["index"] != 0:
                    raise ValueError("invalid stream order")
                delta = choices[0]["delta"]
                if delta.get("role", "assistant") != "assistant":
                    raise ValueError("invalid role")
                budget.write(text, delta.get("content") or "")
                for part in delta.get("tool_calls") or []:
                    index = part["index"]
                    if type(index) is not int or not 0 <= index <= len(calls):
                        raise ValueError("invalid tool index")
                    if index not in calls:
                        empty = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                        # Add the new dictionary entry, excluding its outer braces.
                        budget.add(
                            len(json.dumps({index: empty}).encode("utf-8"))
                            - 2
                            + (2 if calls else 0)
                        )
                        calls[index] = {
                            "id": StringIO(),
                            "type": "function",
                            "function": {"name": StringIO(), "arguments": StringIO()},
                        }
                    call = calls[index]
                    if part.get("type", "function") != "function":
                        raise ValueError("invalid tool type")
                    budget.write(call["id"], part.get("id") or "")
                    for field in ("name", "arguments"):
                        budget.write(
                            call["function"][field], (part.get("function") or {}).get(field) or ""
                        )
                finish = choices[0].get("finish_reason")
        if finish is None or sorted(calls) != list(range(len(calls))):
            raise ValueError("unfinished stream")
        content = text.getvalue()
        for call in calls.values():
            call["id"] = call["id"].getvalue()
            for field in ("name", "arguments"):
                call["function"][field] = call["function"][field].getvalue()
        bounded([content, calls])  # Exact final check, independent of incremental accounting.
        raw: dict[str, Any] = {"role": "assistant", "content": content}
        if calls:
            raw["tool_calls"] = list(calls.values())
        return self._visible(raw), finish


class _StreamBudget:
    """Count serialized bytes once per fragment, retaining no fragment list."""

    def __init__(self) -> None:
        self.size = len(json.dumps(["", {}]).encode("utf-8"))

    def add(self, size: int) -> None:
        self.size += size
        if self.size > MAX_BYTES:
            raise CoreError(413, "history_limit", "Conversation byte limit exceeded")

    def write(self, buffer: StringIO, fragment: str) -> None:
        if not isinstance(fragment, str):
            raise ValueError("invalid stream fragment")
        if not fragment:
            return
        # Reject huge chunks before encoding; every code point needs at least one byte.
        if len(fragment) > MAX_BYTES - self.size:
            raise CoreError(413, "history_limit", "Conversation byte limit exceeded")
        self.add(len(json.dumps(fragment, ensure_ascii=False).encode("utf-8")) - 2)
        buffer.write(fragment)
