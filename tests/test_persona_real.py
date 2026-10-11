"""Real-mode assembly with injected adapters, never a live model."""

import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from digital_souls_core.character import load_characters
from digital_souls_core.history import Binding, HistoryStore, Snapshot
from digital_souls_core.persona_evaluation import FakeJudge, load_cases
from digital_souls_core.persona_fixtures import FixtureHistory, FixtureProvider
from digital_souls_core.persona_real import disposable_config, real_entries, real_metadata
from digital_souls_core.persona_runtime import evaluate
from digital_souls_core.prompt_builder import ByteTokenCounter

ROOT = Path(__file__).resolve().parents[1]


class SyntheticStore(FixtureHistory):
    def __init__(self) -> None:
        super().__init__()
        self.created: list[str] = []

    def create(self, binding: Binding) -> Snapshot:
        self.serial += 1
        cid = f"real-test-{self.serial}"
        self.created.append(cid)
        self.snapshots[cid] = Snapshot(cid, 0, ())
        return self.snapshots[cid]

    def delete(self, binding: Binding, conversation_id: str) -> None:
        self.finish(conversation_id)


@pytest.mark.it1
async def test_real_path_preserves_profile_settings_history_and_report() -> None:
    cases = load_cases(ROOT / "evals/persona/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    profile = char.config.profile.model_copy(
        update={
            "model": "openai/synthetic-real-route",
            "allowed_parameters": frozenset({"temperature", "max_completion_tokens"}),
        }
    )
    char = replace(char, config=char.config.model_copy(update={"profile": profile}))
    provider = FixtureProvider(
        {c.messages[-1].content: " ".join(g[0] for g in c.required) for c in cases.cases}, {}
    )
    store = SyntheticStore()
    settings = {"temperature": 0.25, "max_completion_tokens": 512}
    entries = real_entries(
        char, settings, provider=provider, history_factory=lambda: cast(HistoryStore, store)
    )
    report = await evaluate(cases, entries, judge=FakeJudge(), commit="a" * 40)
    report.update(real_metadata(char, settings))
    assert report["passed"] and report["quality_evidence"] is True
    assert report["mode"] == "real" and report["generation_settings"] == settings
    assert report["profile"]["profile_id"] == profile.profile_id
    assert "api_base" not in report["profile"]
    assert "responses" not in report and report["egogram"]["real_judge"] == "NOT RUN"
    assert report["case_version"] == cases.version
    assert {x["card_sha256"] for x in report["cases"]} == {char.card_sha256}
    assert provider.calls == 18 and len(set(store.created)) == 9 and not store.snapshots
    for payload in provider.payloads:
        assert payload["temperature"] == 0.25 and payload["max_completion_tokens"] == 512
        assert payload["cache"] == {"no-cache": True, "no-store": True}
    assert provider.payloads[4]["messages"][-3:] == [
        m.model_dump() for m in cases.cases[1].messages
    ]


@pytest.mark.it1
@pytest.mark.parametrize(
    "failure", ["provider", "storage", "capability", "denied", "length", "refusal"]
)
async def test_real_errors_are_content_free_and_fail_mandatory_gate(failure: str) -> None:
    cases = load_cases(ROOT / "evals/persona/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    store = SyntheticStore()

    class FailingProvider(FixtureProvider):
        async def complete(self, profile: Any, payload: dict[str, Any]) -> dict[str, Any]:
            if failure == "provider":
                raise TimeoutError("sensitive-provider-marker")
            result = await super().complete(profile, payload)
            if failure == "length":
                result["choices"][0]["finish_reason"] = "length"
            if failure == "refusal":
                result["choices"][0]["message"]["content"] = ""
            return result

    def storage() -> HistoryStore:
        if failure == "storage":
            raise ConnectionError("sensitive-storage-marker")
        return cast(HistoryStore, store)

    if failure == "denied":
        char = replace(
            char,
            config=char.config.model_copy(
                update={
                    "profile": char.config.profile.model_copy(
                        update={"external_send_allowed": False}
                    )
                }
            ),
        )
    settings = {"temperature": 0.2} if failure == "capability" else {}
    provider = FailingProvider({c.messages[-1].content: "answer" for c in cases.cases}, {})
    report = await evaluate(
        cases,
        real_entries(char, settings, provider=provider, history_factory=storage),
        judge=FakeJudge(),
        commit="a" * 40,
    )
    assert not report["passed"]
    assert report["gates"]["execution_incomplete"]["count"] == (9 if failure == "storage" else 18)
    assert "sensitive-" not in json.dumps(report) and not store.snapshots


@pytest.mark.ut
def test_disposable_database_requires_the_helper_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DSC_TEST_POSTGRES_SOCKET",
        "DSC_TEST_POSTGRES_PORT",
        "DSC_TEST_POSTGRES_DATABASE",
        "DSC_TEST_POSTGRES_USER",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError):
        disposable_config()
    monkeypatch.setenv("DSC_TEST_POSTGRES_SOCKET", "127.0.0.1")
    monkeypatch.setenv("DSC_TEST_POSTGRES_PORT", "5432")
    monkeypatch.setenv("DSC_TEST_POSTGRES_DATABASE", "core_synthetic")
    monkeypatch.setenv("DSC_TEST_POSTGRES_USER", "core_synthetic")
    with pytest.raises(ValueError):
        disposable_config()
    monkeypatch.setenv("DSC_TEST_POSTGRES_SOCKET", "/tmp/core-pg.12345678/socket")
    config = disposable_config()
    assert config.host == "/tmp/core-pg.12345678/socket"
    assert config.schema_name.startswith("persona_eval_")
    assert config.schema_name != disposable_config().schema_name
    monkeypatch.setenv("DSC_TEST_POSTGRES_DATABASE", "existing_database")
    with pytest.raises(ValueError):
        disposable_config()


@pytest.mark.it1
async def test_llamacpp_profile_and_counter_are_preserved_with_fake_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_core import application
    from digital_souls_core.character import Profile

    cases = load_cases(ROOT / "evals/persona/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    profile = Profile(
        profile_id="local-test",
        model="openai/gemma4-12b",
        transport="llamacpp_chat",
        api_base="http://127.0.0.1:18081/v1",
        token_counter="llamacpp",
        external_send_allowed=True,
    )
    char = replace(char, config=char.config.model_copy(update={"profile": profile}))
    counter_calls: list[tuple[str, float]] = []
    profiles: list[Profile] = []

    def counter(base: str, timeout: float, **kwargs: Any) -> ByteTokenCounter:
        counter_calls.append((base, timeout))
        return ByteTokenCounter()

    class CaptureProvider(FixtureProvider):
        async def complete(self, selected: Profile, payload: dict[str, Any]) -> dict[str, Any]:
            profiles.append(selected)
            return await super().complete(selected, payload)

    monkeypatch.setattr(application, "LlamaCppTokenCounter", counter)
    provider = CaptureProvider(
        {c.messages[-1].content: " ".join(g[0] for g in c.required) for c in cases.cases}, {}
    )
    store = SyntheticStore()
    report = await evaluate(
        cases,
        real_entries(
            char, {}, provider=provider, history_factory=lambda: cast(HistoryStore, store)
        ),
        judge=FakeJudge(),
        commit="a" * 40,
    )
    assert report["passed"]
    assert counter_calls == [(profile.api_base, profile.timeout_seconds)] * 18
    assert profiles == [profile] * 18
    assert all(p["extra_body"] == {"cache_prompt": False} for p in provider.payloads)


@pytest.mark.it1
@pytest.mark.parametrize("include", [False, True])
def test_real_cli_report_with_injected_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include: bool,
) -> None:
    from digital_souls_core.character import Character
    from digital_souls_core.persona_runtime import Entry

    spec = importlib.util.spec_from_file_location(
        "persona_cli", ROOT / "tools/evaluate-persona-consistency.py"
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cases = load_cases(ROOT / "evals/persona/synthetic.cases.json")
    provider = FixtureProvider(
        {c.messages[-1].content: " ".join(g[0] for g in c.required) for c in cases.cases}, {}
    )
    store = SyntheticStore()

    def injected(char: Character, settings: dict[str, Any]) -> dict[str, Entry]:
        return real_entries(
            char, settings, provider=provider, history_factory=lambda: cast(HistoryStore, store)
        )

    monkeypatch.setattr(cli, "real_entries", injected)
    output = tmp_path / "report.json"
    args = [
        "--mode",
        "real",
        "--characters",
        str(ROOT / "evals/persona/synthetic.characters.json"),
        "--character-id",
        "luma",
        "--cases",
        str(ROOT / "evals/persona/synthetic.cases.json"),
        "--output",
        str(output),
    ]
    if include:
        args.append("--include-responses")
    assert cli.main(args) == 0
    report = json.loads(output.read_text())
    assert report["mode"] == "real" and report["quality_evidence"] is True
    assert report["runs"] == 3 and len(report["commit"]["sha"]) == 40
    assert isinstance(report["commit"]["dirty"], bool)
    assert ("responses" in report) is include


@pytest.mark.postgres
async def test_real_path_uses_disposable_production_history() -> None:
    cases = load_cases(ROOT / "evals/persona/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    provider = FixtureProvider(
        {c.messages[-1].content: " ".join(g[0] for g in c.required) for c in cases.cases}, {}
    )
    report = await evaluate(
        cases, real_entries(char, {}, provider=provider), judge=FakeJudge(), commit="a" * 40
    )
    assert report["passed"] and provider.calls == 18
    assert {c["card_sha256"] for c in report["cases"]} == {char.card_sha256}
    assert provider.payloads[4]["messages"][-3:] == [
        m.model_dump() for m in cases.cases[1].messages
    ]


@pytest.mark.it1
async def test_native_sdk_serializes_cache_prompt_and_bypasses_enabled_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from litellm.caching.caching import Cache
    from openai import AsyncOpenAI

    from digital_souls_core.character import Profile
    from digital_souls_core.provider import LiteLLMProvider, litellm

    from .support import completion

    cases = load_cases(ROOT / "evals/persona/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    profile = Profile(
        profile_id="sdk-test",
        model="openai/gemma4-12b",
        transport="llamacpp_chat",
        api_base="http://127.0.0.1:18081/v1",
        external_send_allowed=True,
        allowed_parameters=frozenset({"temperature", "max_completion_tokens"}),
    )
    char = replace(char, config=char.config.model_copy(update={"profile": profile}))
    sent: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["cache_prompt"] is False
        assert body["temperature"] == 0.25 and body["max_completion_tokens"] == 512
        sent.append(body)
        return httpx.Response(200, json=completion("gemma4-12b"))

    cache = Cache(type="local")
    monkeypatch.setattr(litellm, "cache", cache)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        original = litellm.acompletion

        async def call(**kwargs: Any) -> Any:
            kwargs.pop("client", None)
            sdk = AsyncOpenAI(api_key="local-no-auth", base_url=profile.api_base, http_client=http)
            return await original(**kwargs, client=sdk)

        monkeypatch.setattr(litellm, "acompletion", call)
        store = SyntheticStore()
        entries = real_entries(
            char,
            {"temperature": 0.25, "max_completion_tokens": 512},
            provider=LiteLLMProvider(),
            history_factory=lambda: cast(HistoryStore, store),
        )
        for run in (1, 2):
            observation = await entries["chat"].observe(cases.cases[0], run)
            assert observation.text == "こんにちは"
    assert len(sent) == 2
    assert not cache.cache.cache_dict


@pytest.mark.ut
def test_evaluation_history_policy_is_scoped_and_denies_secrets() -> None:
    from digital_souls_core.character import AccessScope
    from digital_souls_core.contracts import Message
    from digital_souls_core.persona_real import EvaluationPolicy

    binding = Binding(AccessScope(), "synthetic")
    policy = EvaluationPolicy(binding)
    assert policy.allows("create", binding, ())
    assert not policy.allows("create", Binding(AccessScope(), "other-character"), ())
    secret = Message(role="user", content="api_key=sk-synthetic-secret-marker-123456789")
    assert not policy.allows("store", binding, (secret,))


@pytest.mark.it1
@pytest.mark.parametrize("content", ["password=synthetic-denied-marker", '{"ambiguous":'])
@pytest.mark.parametrize("location", ["current", "prior"])
async def test_real_privacy_denies_before_counter_and_provider(
    monkeypatch: pytest.MonkeyPatch, content: str, location: str
) -> None:
    from digital_souls_core import application
    from digital_souls_core.character import Profile
    from digital_souls_core.persona_evaluation import CaseSet

    raw = load_cases(ROOT / "evals/persona/synthetic.cases.json").model_dump()
    for case in raw["cases"]:
        if location == "current":
            case["messages"][-1]["content"] = content
        else:
            case["messages"] = [
                {"role": "user", "content": content},
                {"role": "assistant", "content": "synthetic prior response"},
                case["messages"][-1],
            ]
    cases = CaseSet.model_validate(raw)
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    profile = Profile(
        profile_id="privacy-test",
        model="openai/gemma4-12b",
        transport="llamacpp_chat",
        token_counter="llamacpp",
        api_base="http://127.0.0.1:18081/v1",
        external_send_allowed=True,
    )
    char = replace(char, config=char.config.model_copy(update={"profile": profile}))
    counter_calls = []

    def counter(base: str, timeout: float, **kwargs: Any) -> ByteTokenCounter:
        counter_calls.append(base)
        return ByteTokenCounter()

    monkeypatch.setattr(application, "LlamaCppTokenCounter", counter)
    provider = FixtureProvider(
        {c.messages[-1].content: "synthetic answer" for c in cases.cases}, {}
    )
    store = SyntheticStore()
    report = await evaluate(
        cases,
        real_entries(
            char, {}, provider=provider, history_factory=lambda: cast(HistoryStore, store)
        ),
        judge=FakeJudge(),
        commit="a" * 40,
        include_responses=True,
    )
    assert not counter_calls and provider.calls == 0
    assert not report["passed"] and report["gates"]["execution_incomplete"]["count"] == 18
    assert all(c["execution"] == "ERROR" for c in report["cases"])
    assert content not in json.dumps(report, ensure_ascii=False)
    assert "synthetic answer" not in json.dumps(report)
    assert not store.snapshots
