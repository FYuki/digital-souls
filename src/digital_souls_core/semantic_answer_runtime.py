"""Input-only evaluation through production registration, memory and Inference."""

import hashlib
from collections.abc import AsyncGenerator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .application import CoreError, Inference, Provider
from .character import Character, Profile, load_characters
from .contracts import CompletionInput, Message, Tool
from .local_embedding import LocalEmbedding, LocalEmbeddingProfile
from .memory import MemoryContext
from .memory_ranking import RetrievalPolicy
from .prompt_builder import ByteTokenCounter
from .semantic_answer_external import ExternalAnswerProfile, load_external_profile
from .semantic_evaluation_cases import EvaluationCase, parse_evaluation_inputs
from .semantic_evaluation_runtime import FixtureEmbedding, PreparedCase


class AnswerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    enabled: bool = False
    chat: Profile
    embedding: LocalEmbeddingProfile

    def require_enabled(self) -> None:
        if not self.enabled or not self.embedding.enabled or not self.chat.external_send_allowed:
            raise ValueError("Explicit enabled answer profile required")
        # Evaluation inherits no credentials: the existing no-auth local adapter only.
        if self.chat.transport != "llamacpp_chat":
            raise ValueError("Managed local answer profile required")


def answer_character(
    path: Path, case: EvaluationCase, profile: AnswerProfile | ExternalAnswerProfile | None
) -> Character:
    characters = load_characters(path)
    if len(characters) != 1:
        raise ValueError("One synthetic evaluation character required")
    character = characters[0]
    updates: dict[str, Any] = {"character_id": case.binding.character_id}
    if profile is not None:
        profile.require_enabled()
        updates["profile"] = profile.chat
    return replace(character, config=character.config.model_copy(update=updates))


class FixtureAnswerProvider:
    """Fake only the Provider port. It sees the real production payload."""

    def __init__(self, answer: str, *, error: bool = False) -> None:
        self.answer = answer
        self.error = error
        self.payloads: list[dict[str, Any]] = []

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        if self.error:
            raise CoreError(502, "fixture_error", "Synthetic provider failure")
        return {
            "choices": [{"message": {"role": "assistant", "content": self.answer}}],
        }

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        raise ValueError("Answer evaluation does not stream")
        yield  # pragma: no cover


class CountingAnswerProvider(FixtureAnswerProvider):
    """Count authorized dispatches without retaining payloads or using transport."""

    def __init__(self) -> None:
        super().__init__("")
        self.calls = 0
        self.input_tokens = 0

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.input_tokens += ByteTokenCounter()(
            tuple(Message.model_validate(m) for m in payload["messages"]),
            tools=tuple(Tool.model_validate(t) for t in payload.get("tools", ())),
        )
        return {"choices": [{"message": {"role": "assistant", "content": ""}}]}


async def complete_answer(
    runtime: PreparedCase, character: Character, provider: Provider
) -> dict[str, Any]:
    try:
        memory = MemoryContext(runtime.retrieval)
        inference = Inference(
            (character,), provider, privacy=runtime.retrieval.policy, memory_context=memory
        )
        inference.scope = runtime.case.binding.to_domain().scope
        prepared = await inference.prepare(
            character.config.character_id,
            CompletionInput(messages=[Message(role="user", content=runtime.case.query)]),
            alias=False,
            conversation_id=next(iter(runtime.conversation_ids.values())),
        )
        if runtime.retrieval.failed or len(runtime.retrieval.results) != 1:
            raise ValueError("Retrieval failed")
        selected = runtime.retrieval.results[0]
        retained = selected[: len(selected) - prepared.usage.dropped["memory"]]
        context_empty = not retained
        runtime.mutate("after_search")
        valid = prepared.valid()
        observation = {
            "id": runtime.case.id,
            "answer": None,
            "dispatch_valid": valid,
            "dispatch_memory_ids": [c.identifier for c in retained] if valid else [],
            "dispatched": False,
            "discarded": False,
            "context_empty": context_empty,
        }
        try:
            # The actual Core check is the last step before Provider dispatch.
            inference.check(prepared)
        except CoreError as error:
            if error.code != "context_revoked" or valid:
                raise
            return observation
        result = await provider.complete(prepared.character.config.profile, prepared.payload)
        observation["dispatched"] = True
        runtime.mutate("after_answer")
        try:
            inference.check(prepared)
        except CoreError as error:
            if error.code != "context_revoked":
                raise
            observation["discarded"] = True
            return observation
        choices = result.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("Invalid completion")
        message = choices[0]["message"]
        if message.get("role") != "assistant" or type(message.get("content")) is not str:
            raise ValueError("Invalid completion")
        observation["answer"] = message["content"]
        return observation
    except Exception:
        raise ValueError("Answer evaluation failed") from None


EVAL = Path(__file__).resolve().parents[2] / "evals/semantic"


def load_profile(
    path: str | None, mode: str = "local_model"
) -> AnswerProfile | ExternalAnswerProfile | None:
    if mode == "external_model":
        return load_external_profile(path)
    if path is None:
        return None
    try:
        if not Path(path).is_absolute():
            raise ValueError
        profile = AnswerProfile.model_validate_json(Path(path).read_text(encoding="utf-8"))
        profile.require_enabled()
        return profile
    except Exception:
        raise ValueError("Invalid answer profile") from None


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_identity(mode: str, profile_path: str | None) -> dict[str, Any]:
    if mode not in {"fixture", "local_model", "external_model"} or (
        mode == "fixture" and profile_path is not None
    ):
        raise ValueError("Invalid answer mode")
    profile = load_profile(profile_path, mode)
    if mode == "local_model" and profile is None:
        raise ValueError("Explicit answer profile required")
    inputs = parse_evaluation_inputs((EVAL / "cases.json").read_text(encoding="utf-8"))
    embedding = FixtureEmbedding(inputs) if profile is None else LocalEmbedding(profile.embedding)
    chat = profile.chat if profile else None
    identity = {
        "mode": mode,
        "quality_evidence": mode in {"local_model", "external_model"},
        "classifier": "synthetic",
        "answer_model": {
            "profile_id": chat.profile_id if chat else "fixture-answer",
            "model": chat.model if chat else "openai/fixture-answer",
            "transport": chat.transport if chat else "fake-provider-port",
        },
        "space": asdict(embedding.space),
        "retrieval": asdict(RetrievalPolicy()),
        "cases_sha256": digest(EVAL / "cases.json"),
        "fixtures_sha256": digest(EVAL / "answer-fixtures.json") if profile is None else None,
        "characters_sha256": digest(EVAL / "characters.json"),
        "card_sha256": digest(EVAL / "evaluation.card.json"),
    }

    if mode == "external_model":
        assert chat is not None
        identity.update(
            classifier_reason="baseline-parity-no-classifier-export",
            provider=chat.model.split("/", 1)[0],
            model=chat.model,
            transport=chat.transport,
            external_send=True,
            cache=False,
        )
        if (
            isinstance(profile, ExternalAnswerProfile)
            and profile.answer_provider == "codex_subscription"
        ):
            from .semantic_answer_codex import CONDITION_NOTE

            identity.update(
                provider="codex_subscription",
                model=chat.model.split("/", 1)[1],
                codex_cli_version=None,
                condition_note=CONDITION_NOTE,
            )
    return identity
