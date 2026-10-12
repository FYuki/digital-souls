"""Operator-enabled model evaluation on disposable, scoped PostgreSQL history."""

import os
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any
from uuid import uuid4

from .application import Inference, Provider
from .character import Character, Profile
from .contracts import Message
from .conversations import Conversations
from .history import Binding, HistoryStore
from .persona_evaluation import Case, Observation
from .persona_runtime import ChatEntry, Entry, HistoryEntry
from .postgres_db import PostgresConfig, PostgresDatabase
from .postgres_history import PostgresHistory
from .privacy import PrivacyPolicy
from .provider import LiteLLMProvider


def disposable_config() -> PostgresConfig:
    """Accept only the dedicated socket/names exported by with-test-postgres.sh.

    There is no production DSN option or implicit libpq configuration. The helper
    owns and removes the entire network-disabled container after this command.
    """
    host = os.environ.get("DSC_TEST_POSTGRES_SOCKET", "")
    if (
        re.fullmatch(r"/tmp/core-pg\.[A-Za-z0-9]{8}/socket", host) is None
        or os.environ.get("DSC_TEST_POSTGRES_PORT") != "5432"
        or os.environ.get("DSC_TEST_POSTGRES_DATABASE") != "core_synthetic"
        or os.environ.get("DSC_TEST_POSTGRES_USER") != "core_synthetic"
    ):
        raise ValueError("disposable PostgreSQL helper required")
    return PostgresConfig(
        host=host,
        port=5432,
        database="core_synthetic",
        user="core_synthetic",
        schema_name="persona_eval_" + uuid4().hex,
    )


class UncachedProvider:
    """Disable LiteLLM cache reads/writes after production capability validation."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        options = {**payload, "cache": {"no-cache": True, "no-store": True}, "caching": False}
        if profile.transport == "llamacpp_chat":
            options["extra_body"] = {"cache_prompt": False}
        return await self.provider.complete(profile, options)

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        raise ValueError("evaluation does not stream")
        yield {}  # pragma: no cover


class SyntheticClassifier:
    async def safe(self, value: object, policy_version: str) -> bool:
        return True  # Operator-provided synthetic evaluation data only; scan runs first.


class EvaluationPolicy(PrivacyPolicy):
    """Synthetic consent shared by both evaluations; denied until scoped explicitly."""

    def __init__(self, binding: Binding | None = None) -> None:
        super().__init__(SyntheticClassifier())
        if binding is not None:
            self.consent(binding)

    def consent(self, binding: Binding) -> None:
        self.configure({binding: frozenset({"history", "local", "external", "memory"})})


class EvaluationSession:
    def __init__(self, service: Conversations, character_id: str) -> None:
        self.service = service
        self.character_id = character_id

    def start(self, case: Case) -> tuple[str, int]:
        snapshot = self.service.create(self.character_id)
        cid = snapshot.conversation_id
        try:
            prior = tuple(Message.model_validate(m.model_dump()) for m in case.messages[:-1])
            if prior:
                binding = self.service.binding(self.character_id)
                self.service.authorize("store", binding, prior)
                self.service.store.append(
                    binding, cid, "persona-seed", "synthetic-seed", 0, prior, "stop"
                )
            return cid, int(bool(prior))
        except Exception:
            self.finish(cid)
            raise

    def finish(self, conversation_id: str) -> None:
        self.service.delete(self.character_id, conversation_id)


class RealEntry:
    """A new coordinator/context/session for every case and run."""

    def __init__(
        self,
        character: Character,
        settings: dict[str, Any],
        provider: Provider,
        history_factory: Callable[[], HistoryStore] | None = None,
    ) -> None:
        self.character = character
        self.settings = dict(settings)
        self.provider = provider
        self.history_factory = history_factory

    async def observe(self, case: Case, run: int) -> Observation:
        policy = EvaluationPolicy()
        inference = Inference((self.character,), self.provider, privacy=policy)
        cid = self.character.config.character_id
        policy.consent(Binding(inference.scope, cid))
        if self.history_factory is None:
            return await ChatEntry(inference, cid, self.settings).observe(case, run)
        # Initialize storage within observe: connection/setup failures become
        # content-free ERROR observations and mandatory execution_incomplete FAIL.
        service = Conversations(inference, self.history_factory(), policy)
        return await HistoryEntry(
            service, cid, EvaluationSession(service, cid), self.settings
        ).observe(case, run)


def real_entries(
    character: Character,
    settings: dict[str, Any],
    *,
    provider: Provider | None = None,
    history_factory: Callable[[], HistoryStore] | None = None,
) -> dict[str, Entry]:
    if history_factory is None:
        config = disposable_config()

        def history_factory() -> HistoryStore:
            return PostgresHistory(PostgresDatabase(config))

    uncached = UncachedProvider(provider if provider is not None else LiteLLMProvider())
    return {
        "chat": RealEntry(character, settings, uncached),
        "history": RealEntry(character, settings, uncached, history_factory),
    }


def real_metadata(character: Character, settings: dict[str, Any]) -> dict[str, Any]:
    profile = character.config.profile
    return {
        "mode": "real",
        "quality_evidence": True,
        "cache": False,
        "profile": {
            "profile_id": profile.profile_id,
            "model": profile.model,
            "transport": profile.transport,
            "token_counter": profile.token_counter,
            "allowed_parameters": sorted(profile.allowed_parameters),
            "timeout_seconds": profile.timeout_seconds,
            **({"ollama_think": profile.ollama_think} if profile.ollama_think is not None else {}),
        },
        "model": profile.model,
        "character_id": character.config.character_id,
        "config_version": character.config.config_version,
        "prompt_budget": character.config.prompt_budget.model_dump(),
        "context_floors": {
            "memory": character.config.memory_floor,
            "history": character.config.history_floor,
        },
        "generation_settings": dict(settings),
        "history_storage": "disposable_postgresql",
    }
