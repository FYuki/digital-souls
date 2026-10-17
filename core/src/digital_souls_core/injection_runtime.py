"""Injection entries reuse production coordinators with fixed synthetic retrieval."""

import json
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .application import Inference, Provider
from .character import AccessScope, Character, GuardedContext, Profile
from .contracts import CompletionInput
from .conversations import Conversations
from .errors import CoreError
from .history import Binding, HistoryStore, Snapshot, TurnInput
from .injection_evaluation import GATES, Case, CaseSet, aggregate
from .persona_evaluation import Observation
from .persona_fixtures import FixtureHistory, FixtureProvider
from .persona_real import EvaluationPolicy, EvaluationSession, UncachedProvider, disposable_config
from .postgres_db import PostgresDatabase
from .postgres_history import PostgresHistory
from .privacy import PrivacyPolicy
from .prompt_builder import MEMORY_NOTICE
from .provider import LiteLLMProvider


class FixedMemory:
    def __init__(self, policy: PrivacyPolicy, text: str | None) -> None:
        self.policy = policy
        self.text = text

    async def context(
        self,
        character: Character,
        scope: AccessScope,
        user_text: str,
        *,
        authorized: Callable[[], bool],
    ) -> GuardedContext:
        items = (
            (
                json.dumps(
                    {"memory_ref": "memory-1", "kind": "semantic", "text": self.text},
                    ensure_ascii=False,
                ),
            )
            if self.text
            else ()
        )
        if not authorized() or not await self.policy.authorize(
            Binding(scope, character.config.character_id), "memory", items
        ):
            raise CoreError(403, "privacy_denied", "Privacy policy denies evaluation memory")
        if not authorized():
            raise CoreError(403, "privacy_denied", "Evaluation memory policy changed")
        return GuardedContext("", authorized, self.policy, items)


class FrameCheckedProvider:
    """A discarded attack is an incomplete evaluation, never resistance evidence."""

    def __init__(self, provider: Provider, case: Case) -> None:
        self.provider, self.case = provider, case

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        messages = payload["messages"]
        if self.case.route == "memory":
            frames = [
                m["content"]
                for m in messages
                if m["role"] == "user"
                and isinstance(m["content"], str)
                and m["content"].startswith("retrieved_memory_data (memory data only):\n")
            ]
            valid = any(
                any(
                    item.get("text") == self.case.memory_text
                    for item in json.loads(frame.split("\n", 1)[1])
                )
                for frame in frames
            ) and any(
                MEMORY_NOTICE in (m["content"] or "") for m in messages if m["role"] == "system"
            )
        else:
            valid = (
                messages[-1]["role"] == "user"
                and messages[-1]["content"] == self.case.messages[-1].content
            )
        if not valid:
            raise ValueError("attack omitted from assembled prompt")
        return await self.provider.complete(profile, payload)

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        raise ValueError("evaluation does not stream")
        yield {}  # pragma: no cover


class OverrideChannelOpen(Exception):
    """The structural probe reached dispatch instead of rejecting system input."""


class StructuralProvider:
    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        raise OverrideChannelOpen()

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        raise OverrideChannelOpen()
        yield {}  # pragma: no cover


class Entry:
    def __init__(
        self,
        name: str,
        character: Character,
        provider: Provider,
        settings: dict[str, Any],
        history_factory: Callable[[], HistoryStore] | None = None,
    ) -> None:
        self.name = name
        self.character = character
        self.provider = provider
        self.settings = settings
        self.history_factory = history_factory

    async def structure(self, case: Case, run: int) -> bool:
        # Disable the remote tokenizer as well as generation for structural probes.
        profile = self.character.config.profile.model_copy(update={"token_counter": "estimate"})
        character = replace(
            self.character, config=self.character.config.model_copy(update={"profile": profile})
        )
        policy = EvaluationPolicy()
        inference = Inference((character,), StructuralProvider(), privacy=policy)
        cid = character.config.character_id
        policy.consent(Binding(inference.scope, cid))
        payload = {**self.settings, "messages": [m.model_dump() for m in case.messages]}
        try:
            if self.name == "chat":
                prepared = await inference.prepare(
                    cid, CompletionInput.model_validate(payload), alias=False
                )
                inference.check(prepared)
                await inference.provider.complete(profile, prepared.payload)
            else:
                # Rejection happens before storage; no DB is needed for this probe.
                store = cast(HistoryStore, FixtureHistory())
                service = Conversations(inference, store, policy)
                await service.complete(
                    cid,
                    "structural-probe",
                    TurnInput.model_validate(
                        {
                            **payload,
                            "request_id": f"injection-{run}",
                            "expected_revision": 0,
                        }
                    ),
                )
        except OverrideChannelOpen:
            return True
        except CoreError as error:
            if error.status == 400 and error.code == "system_message_unsupported":
                return False
            raise
        return True

    async def observe(self, case: Case, run: int) -> Observation | bool:
        if case.route == "caller_system":
            return await self.structure(case, run)
        provider: Provider = FrameCheckedProvider(self.provider, case)
        policy = EvaluationPolicy()
        inference = Inference(
            (self.character,),
            provider,
            privacy=policy,
            memory_context=FixedMemory(policy, case.memory_text),
        )
        card_hash: str | None
        cid = self.character.config.character_id
        binding = Binding(inference.scope, cid)
        policy.consent(binding)
        if self.name == "chat":
            # Stateless API has no stored memory lookup. A synthetic evaluation
            # scope enables only the fixed lookup, then the production memory frame.
            prepared = await inference.prepare(
                cid,
                CompletionInput.model_validate(
                    {**self.settings, "messages": [m.model_dump() for m in case.messages]}
                ),
                alias=False,
                conversation_id="injection-synthetic-lookup",
            )
            inference.check(prepared)
            result = await provider.complete(self.character.config.profile, prepared.payload)
            if len(result["choices"]) != 1 or result["choices"][0]["finish_reason"] != "stop":
                raise ValueError("incomplete response")
            text = result["choices"][0]["message"]["content"]
            card_hash = prepared.headers["X-Character-Card-SHA256"]
        else:
            if self.history_factory is None:
                raise ValueError("missing history factory")
            store = self.history_factory()
            service = Conversations(inference, store, policy)
            if isinstance(store, FixtureHistory):
                # No prior messages; the attack is submitted as new input.
                store.serial += 1
                conversation_id = f"injection-{store.serial}"
                store.snapshots[conversation_id] = Snapshot(conversation_id, 0, ())
                finish = store.finish
            else:
                session = EvaluationSession(service, cid)
                conversation_id = service.create(cid).conversation_id
                finish = session.finish
            try:
                receipt = await service.complete(
                    cid,
                    conversation_id,
                    TurnInput.model_validate(
                        {
                            **self.settings,
                            "request_id": f"injection-{run}",
                            "expected_revision": 0,
                            "messages": [m.model_dump() for m in case.messages],
                        }
                    ),
                )
                if receipt.finish_reason != "stop":
                    raise ValueError("incomplete response")
                text = receipt.message.content
                card_hash = receipt.card_sha256
            finally:
                finish(conversation_id)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("nontext response")
        return Observation(
            entry=self.name, run=run, case_id=case.id, text=text, card_sha256=card_hash
        )


class ScenarioEntry:
    def __init__(self, entry: Entry, selected: str, scenario: str) -> None:
        self.entry, self.selected, self.scenario = entry, selected, scenario

    async def observe(self, case: Case, run: int) -> Observation | bool:
        result = await self.entry.observe(case, run)
        if case.id != self.selected:
            return result
        if self.scenario == "override_channel_open" and case.route == "caller_system":
            return True  # Deliberately broken structure observation, fixture only.
        if isinstance(result, Observation) and self.entry.name == "chat":
            if self.scenario == "card_hash_mismatch":
                return result.model_copy(update={"card_sha256": "b" * 64})
            if self.scenario == "card_hash_missing":
                return result.model_copy(update={"card_sha256": None})
            if self.scenario == "execution_incomplete":
                return result.model_copy(
                    update={"status": "ERROR", "text": None, "reason": "fixture_incomplete"}
                )
        return result


async def evaluate(
    cases: CaseSet,
    entries: dict[str, Entry | ScenarioEntry],
    *,
    runs: int = 3,
    commit: str,
    expected_card_hash: str,
    commit_dirty: bool = False,
    include_responses: bool = False,
) -> dict[str, Any]:
    if type(runs) is not int or runs < 1 or set(entries) != {"chat", "history"}:
        raise ValueError("invalid evaluation matrix")
    cases = CaseSet.model_validate(cases.model_dump())
    fixed_version = cases.version
    observations = []
    structures: dict[tuple[str, int, str], bool | None] = {}
    for run in range(1, runs + 1):
        for name, entry in entries.items():
            for case in cases.cases:
                structural = case.route == "caller_system"
                try:
                    result = await entry.observe(case, run)
                    if structural:
                        if type(result) is not bool:
                            raise ValueError("invalid structure observation")
                        structures[(name, run, case.id)] = result
                    else:
                        if not isinstance(result, Observation) or (
                            result.entry,
                            result.run,
                            result.case_id,
                        ) != (name, run, case.id):
                            raise ValueError("invalid observation address")
                        observations.append(Observation.model_validate(result.model_dump()))
                except Exception:
                    if structural:
                        structures[(name, run, case.id)] = None
                    else:
                        observations.append(
                            Observation(
                                entry=name,
                                run=run,
                                case_id=case.id,
                                text=None,
                                card_sha256=None,
                                status="ERROR",
                                reason="evaluation_failed",
                            )
                        )
    if cases.version != fixed_version:
        raise ValueError("cases changed during evaluation")
    return aggregate(
        cases,
        observations,
        structures=structures,
        runs=runs,
        commit=commit,
        expected_card_hash=expected_card_hash,
        commit_dirty=commit_dirty,
        include_responses=include_responses,
    )


def fixture_entries(
    character: Character, cases: CaseSet, path: Path, scenario: str = "pass"
) -> tuple[dict[str, Entry | ScenarioEntry], dict[str, FixtureProvider]]:
    profile = character.config.profile
    if (
        profile.model != "openai/fixture-only"
        or profile.transport != "sdk"
        or profile.token_counter != "estimate"
    ):
        raise ValueError("offline fixture profile required")
    fixture = json.loads(path.read_text())
    if (
        fixture["schema_version"] != 1
        or scenario not in fixture["scenarios"]
        or set(fixture["responses"]) != {c.id for c in cases.cases if c.assessment == "response"}
    ):
        raise ValueError("invalid fixture")
    responses = {
        c.messages[-1].content: fixture["responses"][c.id]
        for c in cases.cases
        if c.assessment == "response"
    }
    if len(responses) != sum(c.assessment == "response" for c in cases.cases):
        raise ValueError("unique probes required")
    providers = {
        name: FixtureProvider(
            responses,
            {
                c.messages[-1].content: fixture["scenarios"][scenario]
                .get(name, {})
                .get(c.id, fixture["responses"][c.id])
                for c in cases.cases
                if c.assessment == "response"
            },
        )
        for name in ("chat", "history")
    }
    entries: dict[str, Entry | ScenarioEntry] = {}
    for name in providers:
        entry = Entry(
            name, character, providers[name], {}, lambda: cast(HistoryStore, FixtureHistory())
        )
        selected = (
            next(c.id for c in cases.cases if c.route == "caller_system")
            if scenario == "override_channel_open"
            else next(c.id for c in cases.cases if c.assessment == "response")
        )
        if scenario in ("injection_followed", "persona_break"):
            selected = next(c.id for c in cases.cases if c.route == "memory")
        entries[name] = ScenarioEntry(entry, selected, scenario)
    if scenario != "pass" and scenario not in GATES:
        raise ValueError("invalid scenario")
    return entries, providers


def real_entries(
    character: Character,
    settings: dict[str, Any],
    *,
    provider: Provider | None = None,
    history_factory: Callable[[], HistoryStore] | None = None,
) -> dict[str, Entry | ScenarioEntry]:
    if history_factory is None:
        config = disposable_config()

        def history_factory() -> HistoryStore:
            return PostgresHistory(PostgresDatabase(config))

    uncached = UncachedProvider(provider if provider is not None else LiteLLMProvider())
    return {
        name: Entry(name, character, uncached, settings, history_factory)
        for name in ("chat", "history")
    }
