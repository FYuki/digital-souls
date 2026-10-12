"""Sequential privacy evaluation through the production LocalClassifier only."""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from pydantic import Field, model_validator

from .application import Provider
from .character import Profile
from .persona_real import UncachedProvider
from .privacy_classifier import (
    CLASSIFIER_VERSION,
    PROMPT_VERSION,
    Assessment,
    AssessmentResult,
    LocalClassifier,
)
from .privacy_classifier_evaluation import Observation, aggregate, validate_options
from .privacy_evaluation_cases import EvaluationCases, Identifier, Strict, Versioned, _unique_object
from .privacy_scan import POLICY_VERSION
from .provider import LiteLLMProvider


class FixtureOverride(Strict):
    action: Literal["assessment", "invalid_output", "timeout", "provider_exception"]
    assessment: Assessment | None = None

    @model_validator(mode="after")
    def consistent(self) -> "FixtureOverride":
        if (self.action == "assessment") != (self.assessment is not None):
            raise ValueError("fixture assessment/action mismatch")
        return self


class FixtureDocument(Versioned):
    responses: dict[Identifier, Assessment] = Field(min_length=1)
    scenarios: dict[Identifier, dict[Identifier, FixtureOverride]]


class FixtureProvider:
    """No transport dependency: resolve only classifier user string to fixture id."""

    def __init__(
        self,
        addresses: dict[str, str],
        responses: dict[str, Assessment],
        overrides: dict[str, FixtureOverride],
    ) -> None:
        self.addresses = dict(addresses)
        self.responses = dict(responses)
        self.overrides = dict(overrides)

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        text = json.loads(payload["messages"][1]["content"])
        if not isinstance(text, str):
            raise ValueError("classifier input must be text only")
        case_id = self.addresses[text]
        override = self.overrides.get(case_id)
        assessment = self.responses[case_id]
        if override:
            if override.action == "timeout":
                raise TimeoutError("synthetic timeout")
            if override.action == "provider_exception":
                raise RuntimeError("synthetic incomplete execution")
            if override.action == "invalid_output":
                return {"choices": []}
            assert override.assessment is not None
            assessment = override.assessment
        return {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": assessment.model_dump_json()},
                }
            ]
        }

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        raise ValueError("privacy fixture does not stream")
        yield {}  # pragma: no cover


def fixture_profile() -> Profile:
    """Compatible synthetic local profile; the fixture provider never sends it."""
    return Profile(
        profile_id="privacy-fixture",
        model="openai/gemma4-12b",
        transport="llamacpp_chat",
        api_base="http://127.0.0.1:18080/v1",
        external_send_allowed=True,
        allowed_parameters=frozenset({"max_completion_tokens"}),
    )


def fixture_provider(cases: EvaluationCases, path: Path, scenario: str) -> FixtureProvider:
    try:
        fixture = FixtureDocument.model_validate(
            json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
        )
        ids = {c.id for c in cases.cases.cases}
        addresses = {c.text: c.id for c in cases.cases.cases}
        if (
            set(fixture.responses) != ids
            or len(addresses) != len(ids)
            or scenario not in fixture.scenarios
            or any(not set(s) <= ids for s in fixture.scenarios.values())
        ):
            raise ValueError("invalid fixture correspondence")
        return FixtureProvider(addresses, fixture.responses, fixture.scenarios[scenario])
    except Exception:
        raise ValueError("invalid privacy fixture") from None


def real_provider(provider: Provider | None = None) -> UncachedProvider:
    return UncachedProvider(provider if provider is not None else LiteLLMProvider())


async def evaluate(
    cases: EvaluationCases,
    provider: Provider,
    profile: Profile,
    *,
    model_digest: str,
    mode: Literal["fixture", "real"],
    runs: int,
    abstain_policy: str,
    commit: str,
    commit_dirty: bool = False,
) -> dict[str, Any]:
    validate_options(runs, abstain_policy)
    if mode not in {"fixture", "real"} or (
        mode == "fixture" and type(provider) is not FixtureProvider
    ):
        raise ValueError("fixture mode requires offline fixture provider")
    if mode == "real" and not isinstance(provider, UncachedProvider):
        raise ValueError("real mode requires uncached provider")
    profile = Profile.model_validate(profile.model_dump(exclude_none=True))
    # Fail configuration errors before any case or transport is executed.
    LocalClassifier(provider, profile, model_digest=model_digest)
    cases = EvaluationCases.model_validate(cases.model_dump())
    fixed_version = cases.version
    started_at = datetime.now(UTC).isoformat()
    observations = []
    for run in range(1, runs + 1):
        for case in cases.cases.cases:
            started = perf_counter()
            try:
                result = await LocalClassifier(provider, profile, model_digest=model_digest).assess(
                    case.text, POLICY_VERSION
                )
            except Exception:
                result = AssessmentResult(failure="provider_exception")
            observations.append(
                Observation(
                    run=run,
                    case_id=case.id,
                    result=result,
                    latency_ms=(perf_counter() - started) * 1000,
                )
            )
    if fixed_version != cases.version:
        raise ValueError("privacy cases changed during evaluation")
    report = aggregate(cases, observations, runs=runs, abstain_policy=abstain_policy)
    report.update(
        {
            "mode": mode,
            "quality_evidence": mode == "real",
            "cache": False,
            "commit": {"sha": commit, "dirty": commit_dirty},
            "classifier_version": CLASSIFIER_VERSION,
            "prompt_version": PROMPT_VERSION,
            "policy_version": POLICY_VERSION,
            "model": profile.model,
            "model_digest": model_digest,
            "profile": {
                "profile_id": profile.profile_id,
                "model": profile.model,
                "transport": profile.transport,
                "token_counter": profile.token_counter,
                "allowed_parameters": sorted(profile.allowed_parameters),
                "timeout_seconds": profile.timeout_seconds,
            },
            "parallelism": 1,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )
    return report
