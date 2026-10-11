"""Offline production assembly and CLI zero-tolerance gates."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from digital_souls_core.character import load_characters
from digital_souls_core.injection_evaluation import load_cases
from digital_souls_core.injection_runtime import evaluate, fixture_entries

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.it1


@pytest.mark.parametrize("content", ["password=synthetic-denied-marker", '{"ambiguous":'])
async def test_real_privacy_denies_before_counter_and_provider(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    from dataclasses import replace
    from typing import Any, cast

    from digital_souls_core import application
    from digital_souls_core.character import Profile
    from digital_souls_core.history import HistoryStore
    from digital_souls_core.injection_evaluation import CaseSet
    from digital_souls_core.injection_runtime import real_entries
    from digital_souls_core.persona_fixtures import FixtureHistory, FixtureProvider
    from digital_souls_core.prompt_builder import ByteTokenCounter

    raw = load_cases(ROOT / "evals/injection/synthetic.cases.json").model_dump()
    for case in raw["cases"]:
        if case["assessment"] == "response":
            original = case["messages"][-1]["content"]
            case["messages"][-1]["content"] = (
                original + "\n" + content
                if content.startswith("password")
                else '{"attack":' + json.dumps(original) + ","
            )
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
    store = FixtureHistory()
    report = await evaluate(
        cases,
        real_entries(
            char, {}, provider=provider, history_factory=lambda: cast(HistoryStore, store)
        ),
        runs=3,
        commit="a" * 40,
        expected_card_hash=char.card_sha256,
        include_responses=True,
    )
    assert not counter_calls and provider.calls == 0
    assert not report["passed"] and report["gates"]["execution_incomplete"]["count"] == 12
    assert "synthetic-denied-marker" not in json.dumps(report)
    assert "synthetic answer" not in json.dumps(report)
    assert not store.snapshots


@pytest.mark.parametrize("name", ["chat", "history"])
@pytest.mark.parametrize("content", ["password=synthetic-denied-marker", '{"ambiguous":'])
async def test_real_memory_privacy_precedes_tokenizer_send(
    monkeypatch: pytest.MonkeyPatch, name: str, content: str
) -> None:
    from collections.abc import Awaitable, Callable, Sequence
    from dataclasses import replace
    from typing import Any, cast

    from digital_souls_core import application
    from digital_souls_core.character import Profile
    from digital_souls_core.contracts import Message, Tool
    from digital_souls_core.errors import CoreError
    from digital_souls_core.history import HistoryStore
    from digital_souls_core.injection_runtime import real_entries
    from digital_souls_core.persona_fixtures import FixtureHistory, FixtureProvider
    from digital_souls_core.prompt_builder import ByteTokenCounter

    sent: list[dict[str, Any]] = []

    class Counter:
        def __init__(
            self,
            base: str,
            timeout: float,
            *,
            before_send: Callable[[dict[str, Any]], Awaitable[None]],
            **kwargs: Any,
        ) -> None:
            self.before_send = before_send

        async def __call__(self, messages: Sequence[Message], *, tools: Sequence[Tool] = ()) -> int:
            payload = {"messages": [m.model_dump(exclude_none=True) for m in messages]}
            await self.before_send(payload)
            sent.append(payload)
            return ByteTokenCounter()(messages, tools=tools)

    monkeypatch.setattr(application, "LlamaCppTokenCounter", Counter)
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    case = next(c for c in cases.cases if c.route == "memory")
    attack = (
        (case.memory_text or "") + "\n" + content
        if content.startswith("password")
        else '{"attack":' + json.dumps(case.memory_text) + ","
    )
    case = case.model_copy(update={"memory_text": attack})
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
    provider = FixtureProvider({case.messages[-1].content: "synthetic answer"}, {})
    store = FixtureHistory()
    entries = real_entries(
        char, {}, provider=provider, history_factory=lambda: cast(HistoryStore, store)
    )
    with pytest.raises(CoreError) as error:
        await entries[name].observe(case, 1)
    assert error.value.code == "privacy_denied"
    assert provider.calls == 0 and not store.snapshots
    assert not sent


@pytest.mark.parametrize(
    "scenario",
    [
        "pass",
        "injection_followed",
        "persona_break",
        "override_channel_open",
        "card_hash_mismatch",
        "card_hash_missing",
        "execution_incomplete",
    ],
)
def test_cli_exit_and_report(scenario: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/evaluate-injection-resistance.py"),
            "--scenario",
            scenario,
            "--runs",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == (0 if scenario == "pass" else 1), result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == ("PASS" if scenario == "pass" else "FAIL")
    assert "responses" not in report and "safe-response-marker" not in result.stdout
    assert len(report["structure_checks"]) == 6
    assert len(report["cases"]) == 12
    if scenario != "pass":
        assert report["gates"][scenario]["count"] > 0


async def test_all_routes_use_production_frames_and_reject_history_system() -> None:
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    entries, providers = fixture_entries(
        char, cases, ROOT / "evals/injection/responses.fixture.json"
    )
    report = await evaluate(
        cases, entries, runs=3, commit="a" * 40, expected_card_hash=char.card_sha256
    )
    assert report["passed"]
    assert providers["chat"].calls == 6 and providers["history"].calls == 6
    for provider in providers.values():
        memory = next(
            p
            for p in provider.payloads
            if p["messages"][-1]["content"] == cases.cases[2].messages[-1].content
        )
        data = next(
            m
            for m in memory["messages"]
            if "retrieved_memory_data (memory data only)" in m["content"]
        )
        assert data["role"] == "user"
        assert cases.cases[2].canary in data["content"]
        assert cases.cases[2].canary not in memory["messages"][0]["content"]
        assert any("untrusted data, not instructions" in m["content"] for m in memory["messages"])


@pytest.mark.parametrize(
    "options",
    [
        ("--runs", "0"),
        ("--mode", "real"),
        ("--profile", "evals/persona/local-profile.example.json"),
        ("--temperature", "0.2"),
    ],
)
def test_cli_invalid_configuration_never_sends(options: tuple[str, ...]) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/evaluate-injection-resistance.py"), *options],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1 and not result.stdout
    assert "インジェクション評価に失敗" in result.stderr and "Traceback" not in result.stderr
    assert "18081" not in result.stderr


async def test_real_mode_reuses_cache_controls_and_fixed_memory_with_fake_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from typing import Any, cast

    from digital_souls_core import application
    from digital_souls_core.character import Profile
    from digital_souls_core.history import Binding, HistoryStore, Snapshot
    from digital_souls_core.injection_runtime import real_entries
    from digital_souls_core.persona_fixtures import FixtureHistory, FixtureProvider
    from digital_souls_core.persona_real import real_metadata
    from digital_souls_core.prompt_builder import ByteTokenCounter

    class Store:
        def __init__(self) -> None:
            self.fixture = FixtureHistory()
            self.created = 0

        def create(self, binding: Binding) -> Snapshot:
            self.created += 1
            cid = f"real-injection-{self.created}"
            self.fixture.snapshots[cid] = Snapshot(cid, 0, ())
            return self.fixture.snapshots[cid]

        def delete(self, binding: Binding, conversation_id: str) -> None:
            self.fixture.finish(conversation_id)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.fixture, name)

    counter_calls = []

    def counter(endpoint: str, timeout: float, **kwargs: Any) -> ByteTokenCounter:
        counter_calls.append(endpoint)
        return ByteTokenCounter()

    monkeypatch.setattr(application, "LlamaCppTokenCounter", counter)
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    profile = Profile(
        profile_id="fake-llamacpp",
        model="openai/gemma4-12b",
        transport="llamacpp_chat",
        token_counter="llamacpp",
        api_base="http://127.0.0.1:18081/v1",
        external_send_allowed=True,
        allowed_parameters=frozenset({"temperature", "max_completion_tokens"}),
    )
    char = replace(char, config=char.config.model_copy(update={"profile": profile}))
    provider = FixtureProvider(
        {c.messages[-1].content: "safe-response-marker" for c in cases.cases}, {}
    )
    store = Store()
    settings = {"temperature": 0.7, "max_completion_tokens": 1024}
    entries = real_entries(
        char, settings, provider=provider, history_factory=lambda: cast(HistoryStore, store)
    )
    report = await evaluate(
        cases, entries, runs=3, commit="a" * 40, expected_card_hash=char.card_sha256
    )
    report.update(real_metadata(char, settings))
    assert report["passed"] and report["mode"] == "real"
    assert len(counter_calls) == provider.calls == 12 and store.created == 6
    assert not store.fixture.snapshots
    assert (
        report["generation_settings"] == settings
        and report["profile"]["token_counter"] == "llamacpp"
    )
    for payload in provider.payloads:
        assert payload["cache"] == {"no-cache": True, "no-store": True}
        assert payload["caching"] is False and payload["extra_body"] == {"cache_prompt": False}
        assert payload["temperature"] == 0.7
    memories = [
        p
        for p in provider.payloads
        if p["messages"][-1]["content"] == cases.cases[2].messages[-1].content
    ]
    assert len(memories) == 6
    assert all(
        any(
            cases.cases[2].canary in m["content"] and "retrieved_memory_data" in m["content"]
            for m in p["messages"]
        )
        for p in memories
    )
    assert "safe-response-marker" not in json.dumps(report)


@pytest.mark.parametrize("route", ["memory", "user"])
async def test_discarded_attack_cannot_be_reported_as_resistance(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    from dataclasses import replace
    from typing import Any

    from digital_souls_core.prompt_builder import PromptBuilder, PromptInput

    original = PromptBuilder.build

    async def discard(self: Any, data: PromptInput, budgets: Any) -> Any:
        if route == "memory":
            data = replace(data, memory=())
        elif data.current_input and "synthetic-user-1" in (data.current_input[-1].content or ""):
            data = replace(data, current_input=())
        return await original(self, data, budgets)

    monkeypatch.setattr(PromptBuilder, "build", discard)
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    entries, _ = fixture_entries(char, cases, ROOT / "evals/injection/responses.fixture.json")
    report = await evaluate(
        cases, entries, runs=3, commit="a" * 40, expected_card_hash=char.card_sha256
    )
    assert not report["passed"]
    assert report["gates"]["execution_incomplete"]["count"] == 6


@pytest.mark.parametrize("behavior", ["accepted", "privacy_denied", "unavailable"])
async def test_history_structure_distinguishes_acceptance_from_other_errors(
    monkeypatch: pytest.MonkeyPatch, behavior: str
) -> None:
    from digital_souls_core.contracts import Message
    from digital_souls_core.conversations import Conversations
    from digital_souls_core.errors import CoreError
    from digital_souls_core.history import Receipt, TurnInput

    original = Conversations.complete

    async def complete(
        self: Conversations, character_id: str, conversation_id: str, body: TurnInput
    ) -> Receipt:
        if any(m.role == "system" for m in body.messages):
            if behavior == "accepted":
                return Receipt(
                    "synthetic",
                    1,
                    Message(role="assistant", content="safe-response-marker"),
                    "stop",
                    card_sha256="a" * 64,
                )
            raise CoreError(
                403 if behavior == "privacy_denied" else 503, behavior, "sensitive-error-marker"
            )
        return await original(self, character_id, conversation_id, body)

    monkeypatch.setattr(Conversations, "complete", complete)
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    entries, _ = fixture_entries(char, cases, ROOT / "evals/injection/responses.fixture.json")
    report = await evaluate(
        cases, entries, runs=3, commit="a" * 40, expected_card_hash=char.card_sha256
    )
    assert not report["passed"]
    gate = "override_channel_open" if behavior == "accepted" else "execution_incomplete"
    assert report["gates"][gate]["count"] == 3
    assert "sensitive-error-marker" not in json.dumps(report)


@pytest.mark.parametrize(
    "name,case_index", [("chat", 1), ("chat", 2), ("history", 1), ("history", 2)]
)
async def test_canary_gate_depends_on_route_for_both_entries(
    tmp_path: Path, name: str, case_index: int
) -> None:
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    fixture = json.loads((ROOT / "evals/injection/responses.fixture.json").read_text())
    case = cases.cases[case_index]
    fixture["scenarios"]["pass"] = {name: {case.id: case.canary}}
    path = tmp_path / "responses.json"
    path.write_text(json.dumps(fixture))
    entries, _ = fixture_entries(char, cases, path)
    report = await evaluate(
        cases, entries, runs=3, commit="a" * 40, expected_card_hash=char.card_sha256
    )
    assert report["passed"] == (case.route == "user")
    assert report["gates"]["injection_followed"]["count"] == (0 if case.route == "user" else 3)
    if case.route == "user":
        assert report["canary_reference"]["count"] == 3
    else:
        assert {v["entry"] for v in report["gates"]["injection_followed"]["violations"]} == {name}


async def test_open_history_channel_is_detected_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_core.conversations import Conversations
    from digital_souls_core.history import Receipt, TurnInput

    original = Conversations.complete

    async def open_channel(
        self: Conversations, character_id: str, conversation_id: str, body: TurnInput
    ) -> Receipt:
        if any(m.role == "system" for m in body.messages):
            char = self.inference.characters[character_id]
            await self.inference.provider.complete(
                char.config.profile, {"messages": [m.model_dump() for m in body.messages]}
            )
            raise ValueError("synthetic broken coordinator")
        return await original(self, character_id, conversation_id, body)

    monkeypatch.setattr(Conversations, "complete", open_channel)
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    entries, providers = fixture_entries(
        char, cases, ROOT / "evals/injection/responses.fixture.json"
    )
    report = await evaluate(
        cases, entries, runs=3, commit="a" * 40, expected_card_hash=char.card_sha256
    )
    assert report["gates"]["override_channel_open"]["count"] == 3
    assert report["gates"]["execution_incomplete"]["count"] == 0
    assert providers["history"].calls == 6


@pytest.mark.parametrize("filename", ["synthetic.cases.json", "miori.cases.json"])
@pytest.mark.parametrize("name", ["chat", "history"])
@pytest.mark.parametrize("body", ["canary", "persona_break", "both"])
def test_cli_user_roleplay_and_canary_are_reference(
    tmp_path: Path, filename: str, name: str, body: str
) -> None:
    cases = load_cases(ROOT / "evals/injection" / filename)
    fixture = {
        "schema_version": 1,
        "responses": {
            c.id: "safe-response-marker" for c in cases.cases if c.assessment == "response"
        },
        "scenarios": {
            "pass": {
                name: {
                    c.id: c.canary
                    if body == "canary"
                    else "Zeroです"
                    if body == "persona_break"
                    else c.canary + " Zeroです"
                    for c in cases.cases
                    if c.route == "user" and "Zero" in c.messages[-1].content
                }
            }
        },
    }
    path = tmp_path / "responses.json"
    path.write_text(json.dumps(fixture))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/evaluate-injection-resistance.py"),
            "--cases",
            str(ROOT / "evals/injection" / filename),
            "--fixture",
            str(path),
            "--runs",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["passed"]
    assert report["case_version"] == cases.version and not report["cases_draft"]
    assert report["gates"]["injection_followed"]["count"] == 0
    assert report["gates"]["persona_break"]["count"] == 0
    assert report["canary_reference"]["count"] == (0 if body == "persona_break" else 3)
    assert report["persona_break_reference"]["count"] == (0 if body == "canary" else 3)


@pytest.mark.parametrize("name,route", [("chat", "memory"), ("history", "memory")])
@pytest.mark.parametrize("body", ["canary", "persona_break"])
def test_cli_external_content_gate_is_failure(
    tmp_path: Path, name: str, route: str, body: str
) -> None:
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    case = next(c for c in cases.cases if c.route == route)
    fixture = json.loads((ROOT / "evals/injection/responses.fixture.json").read_text())
    fixture["scenarios"]["pass"] = {
        name: {case.id: case.canary if body == "canary" else case.persona_break[0]}
    }
    path = tmp_path / "responses.json"
    path.write_text(json.dumps(fixture))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/evaluate-injection-resistance.py"),
            "--fixture",
            str(path),
            "--runs",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    gate = "injection_followed" if body == "canary" else "persona_break"
    assert report["status"] == "FAIL" and report["gates"][gate]["count"] == 3


def test_cli_empty_cases_cannot_pass(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from digital_souls_core.injection_evaluation import CaseSet

    path = tmp_path / "empty.cases.json"
    path.write_text(json.dumps({"schema_version": 2, "cases": []}))
    with pytest.raises(ValidationError) as error:
        CaseSet.model_validate_json(path.read_bytes())
    assert [(e["loc"], e["type"]) for e in error.value.errors()] == [(("cases",), "too_short")]
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/evaluate-injection-resistance.py"),
            "--cases",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1 and not result.stdout
    assert "インジェクション評価に失敗" in result.stderr


@pytest.mark.parametrize(
    "gate", ["card_hash_missing", "card_hash_mismatch", "execution_incomplete"]
)
def test_cli_user_roleplay_keeps_noncontent_gates(tmp_path: Path, gate: str) -> None:
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    # The fixture scenario alters the first case, so deliberately select user.
    raw = cases.model_dump()
    raw["cases"].sort(key=lambda c: c["route"] != "user")
    case_path = tmp_path / "user-first.cases.json"
    case_path.write_text(json.dumps(raw))
    fixture = json.loads((ROOT / "evals/injection/responses.fixture.json").read_text())
    user = next(c for c in cases.cases if c.route == "user")
    fixture["responses"][user.id] = user.persona_break[0]
    path = tmp_path / "responses.json"
    path.write_text(json.dumps(fixture))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/evaluate-injection-resistance.py"),
            "--cases",
            str(case_path),
            "--fixture",
            str(path),
            "--scenario",
            gate,
            "--runs",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "FAIL" and report["gates"][gate]["count"] == 3
    assert report["gates"]["persona_break"]["count"] == 0
    assert all(x["case_id"] == user.id for x in report["gates"][gate]["violations"])


async def test_caller_system_is_structure_in_both_entries_without_provider_calls() -> None:
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    entries, providers = fixture_entries(
        char, cases, ROOT / "evals/injection/responses.fixture.json"
    )
    report = await evaluate(
        cases, entries, runs=3, commit="a" * 40, expected_card_hash=char.card_sha256
    )
    assert report["passed"]
    assert len(report["structure_checks"]) == 6
    assert {x["entry"] for x in report["structure_checks"]} == {"chat", "history"}
    assert all(x["route"] != "caller_system" for x in report["cases"])
    assert all(p.calls == 6 for p in providers.values())


@pytest.mark.parametrize("behavior", ["accepted", "privacy_denied", "unavailable", "wrong_code"])
async def test_chat_structure_detects_acceptance_and_only_exact_rejection_passes(
    monkeypatch: pytest.MonkeyPatch, behavior: str
) -> None:
    from typing import Any

    from digital_souls_core.application import Inference, Prepared
    from digital_souls_core.contracts import CompletionInput
    from digital_souls_core.errors import CoreError

    original = Inference.prepare

    async def prepare(
        self: Inference, selector: str, request: CompletionInput, **kwargs: Any
    ) -> Prepared:
        if any(m.role == "system" for m in request.messages):
            if behavior != "accepted":
                status = (
                    400
                    if behavior == "wrong_code"
                    else 403
                    if behavior == "privacy_denied"
                    else 503
                )
                raise CoreError(
                    status,
                    "invalid_turn" if behavior == "wrong_code" else behavior,
                    "private-marker",
                )
            request = request.model_copy(
                update={"messages": [m for m in request.messages if m.role != "system"]}
            )
        return await original(self, selector, request, **kwargs)

    monkeypatch.setattr(Inference, "prepare", prepare)
    cases = load_cases(ROOT / "evals/injection/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    entries, providers = fixture_entries(
        char, cases, ROOT / "evals/injection/responses.fixture.json"
    )
    report = await evaluate(
        cases, entries, runs=3, commit="a" * 40, expected_card_hash=char.card_sha256
    )
    gate = "override_channel_open" if behavior == "accepted" else "execution_incomplete"
    assert not report["passed"] and report["gates"][gate]["count"] == 3
    assert {x["entry"] for x in report["gates"][gate]["violations"]} == {"chat"}
    assert "private-marker" not in json.dumps(report)
    assert providers["chat"].calls == providers["history"].calls == 6
