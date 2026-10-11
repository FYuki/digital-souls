"""Offline-only evaluation fixtures, never a production persistence adapter."""

import json
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .application import Inference
from .character import Character, Profile
from .contracts import Message
from .conversations import Conversations
from .history import Binding, HistoryStore, Operation, Receipt, Snapshot
from .persona_evaluation import Case, CaseSet, Observation
from .persona_runtime import ChatEntry, Entry, HistoryEntry


class FixtureProvider:
    def __init__(self, responses: dict[str, str], overrides: dict[str, str]) -> None:
        self.responses = responses
        self.overrides = overrides
        self.calls = 0
        self.payloads: list[dict[str, Any]] = []

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.payloads.append(payload)
        query = payload["messages"][-1]["content"]
        answer = self.overrides.get(query, self.responses[query])
        return {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ]
        }

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        raise ValueError("fixture does not stream")
        yield {}  # pragma: no cover


class FixturePolicy:
    def allows(self, operation: Operation, binding: Binding, messages: tuple[Message, ...]) -> bool:
        return True  # Synthetic fixture only, installed by the fixture CLI.


class FixtureHistory:
    """Only coordinator operations needed by this evaluation are implemented.

    No disk writes, cache or retry receipts.
    """

    def __init__(self) -> None:
        self.snapshots: dict[str, Snapshot] = {}
        self.serial = 0

    def start(self, case: Case) -> tuple[str, int]:
        self.serial += 1
        cid = f"fixture-{self.serial}"
        prior = tuple(Message.model_validate(m.model_dump()) for m in case.messages[:-1])
        self.snapshots[cid] = Snapshot(cid, int(bool(prior)), prior)
        return cid, int(bool(prior))

    def finish(self, conversation_id: str) -> None:
        del self.snapshots[conversation_id]

    def read(self, binding: Binding, conversation_id: str) -> Snapshot:
        return self.snapshots[conversation_id]

    def receipt(self, binding: Binding, conversation_id: str, request_id: str) -> Receipt | None:
        return None

    def append(
        self,
        binding: Binding,
        conversation_id: str,
        request_id: str,
        fingerprint: str,
        expected_revision: int,
        messages: tuple[Message, ...],
        finish_reason: str,
        *,
        memory_excluded_indices: tuple[int, ...] = (),
        memory_confirmation_indices: tuple[int, ...] = (),
        card_sha256: str | None = None,
    ) -> Receipt:
        prior = self.snapshots[conversation_id]
        if prior.revision != expected_revision:
            raise ValueError("fixture revision conflict")
        revision = expected_revision + 1
        self.snapshots[conversation_id] = Snapshot(
            conversation_id, revision, (*prior.messages, *messages)
        )
        return Receipt(fingerprint, revision, messages[-1], finish_reason, card_sha256=card_sha256)


class HashFixtureEntry:
    def __init__(self, character: Character, provider: FixtureProvider, selected: str) -> None:
        self.character = character
        self.provider = provider
        self.selected = selected

    async def observe(self, case: Case, run: int) -> Observation:
        char = (
            replace(self.character, card_sha256="b" * 64)
            if case.id == self.selected
            else self.character
        )
        return await ChatEntry(Inference((char,), self.provider), char.config.character_id).observe(
            case, run
        )


class ObservationFixtureEntry:
    """Deliberately incomplete metadata/result for negative fixture gates."""

    def __init__(self, entry: Entry, selected: str, scenario: str) -> None:
        self.entry = entry
        self.selected = selected
        self.scenario = scenario

    async def observe(self, case: Case, run: int) -> Observation:
        observation = await self.entry.observe(case, run)
        if case.id != self.selected:
            return observation
        if self.scenario == "card_hash_missing":
            return observation.model_copy(update={"card_sha256": None})
        return observation.model_copy(
            update={"text": None, "status": "ERROR", "reason": "fixture_incomplete"}
        )


def fixture_entries(
    character: Character, cases: CaseSet, path: Path, scenario: str = "pass"
) -> tuple[dict[str, Entry], dict[str, FixtureProvider]]:
    fixture = json.loads(path.read_text())
    if fixture["schema_version"] != 1 or scenario not in fixture["scenarios"]:
        raise ValueError("invalid fixture scenario")
    if set(fixture["responses"]) != {c.id for c in cases.cases}:
        raise ValueError("fixture/case mismatch")
    responses = {c.messages[-1].content: fixture["responses"][c.id] for c in cases.cases}
    if len(responses) != len(cases.cases):
        raise ValueError("fixture probes must be unique")
    providers = {}
    for name in ("chat", "history"):
        overrides = {
            c.messages[-1].content: fixture["scenarios"][scenario]
            .get(name, {})
            .get(c.id, fixture["responses"][c.id])
            for c in cases.cases
        }
        providers[name] = FixtureProvider(responses, overrides)
    store = FixtureHistory()
    inference = Inference((character,), providers["history"])
    history = HistoryEntry(
        Conversations(inference, cast(HistoryStore, store), FixturePolicy()),
        character.config.character_id,
        store,
    )
    chat: Entry = ChatEntry(
        Inference((character,), providers["chat"]), character.config.character_id
    )
    if scenario == "card_hash_mismatch":
        chat = HashFixtureEntry(character, providers["chat"], cases.cases[0].id)
    if scenario in ("card_hash_missing", "execution_incomplete"):
        chat = ObservationFixtureEntry(chat, cases.cases[0].id, scenario)
    return {"chat": chat, "history": history}, providers
