"""Fixture CLI and real-mode assembly, with no sockets or actual model transport."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from digital_souls_core.privacy_classifier_runtime import (
    FixtureProvider,
    evaluate,
    fixture_profile,
    fixture_provider,
    real_provider,
)
from digital_souls_core.privacy_evaluation_cases import load_cases

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools/evaluate-privacy-classifier.py"
pytestmark = pytest.mark.it1


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=30, check=False
    )


@pytest.mark.parametrize("policy", ["accept", "reject", "separate"])
def test_cli_pass_three_rounds(policy: str, tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    result = run("--abstain-policy", policy, "--runs", "3", "--output", str(output))
    assert result.returncode == 0 and "PASS" in result.stdout and not result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "PASS" and report["quality_evidence"] is False
    assert report["runs"] == 3 and all(len(r["cases"]) == 60 for r in report["rounds"])
    assert len(report["commit"]["sha"]) == 40 and type(report["commit"]["dirty"]) is bool
    assert report["cache"] is False and report["abstain_policy"] == policy
    assert report["started_at"] <= report["finished_at"]
    assert report["classifier_version"] and report["prompt_version"] and report["policy_version"]
    raw = output.read_text(encoding="utf-8")
    assert all(
        c["text"] not in raw
        for c in json.loads((ROOT / "evals/privacy/cases.json").read_text(encoding="utf-8"))[
            "cases"
        ]
    )
    assert "127.0.0.1" not in raw and "api_base" not in raw and "characters.json" not in raw


@pytest.mark.parametrize(
    "scenario",
    ["false_negative", "invalid_output", "timeout", "execution_incomplete", "group_accuracy"],
)
def test_cli_violation_scenarios_fail(scenario: str) -> None:
    result = run("--abstain-policy", "accept", "--runs", "3", "--scenario", scenario)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "FAIL" and report["fail_reasons"]
    if scenario != "group_accuracy":
        assert report["gates"][scenario]["count"] == 3
    else:
        assert report["rounds"][0]["groups"]["SELF_HARM"]["accuracy"] == 7 / 8


@pytest.mark.parametrize(
    "scenario,policy,code",
    [
        ("abstain_5", "separate", 0),
        ("abstain_6", "separate", 1),
        ("abstain_6", "accept", 0),
        ("abstain_6", "reject", 1),
    ],
)
def test_cli_abstain_policy_boundary(scenario: str, policy: str, code: int) -> None:
    result = run("--abstain-policy", policy, "--runs", "3", "--scenario", scenario)
    assert result.returncode == code
    report = json.loads(result.stdout)
    count = int(scenario[-1])
    assert all(r["abstain"]["count"] == count for r in report["rounds"])
    assert all(g["count"] == 0 for g in report["gates"].values())


@pytest.mark.parametrize(
    "args",
    [
        ("--abstain-policy", "accept", "--runs", "0"),
        ("--abstain-policy", "accept", "--runs", "2"),
        ("--mode", "real", "--abstain-policy", "accept"),
        (
            "--mode",
            "real",
            "--abstain-policy",
            "accept",
            "--runs",
            "3",
            "--characters",
            "unused",
            "--character-id",
            "miori",
            "--model-digest",
            " ",
        ),
    ],
)
def test_cli_requires_complete_arguments(args: tuple[str, ...]) -> None:
    result = run(*args)
    assert result.returncode == 2 and not result.stdout and "Traceback" not in result.stderr


async def test_fixture_retains_classifier_and_never_creates_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import digital_souls_core.privacy_classifier_runtime as runtime

    def forbidden() -> None:
        raise AssertionError("fixture attempted real provider")

    monkeypatch.setattr(runtime, "LiteLLMProvider", forbidden)
    cases = load_cases(ROOT / "evals/privacy/cases.json", ROOT / "evals/privacy/expectations.json")
    provider = fixture_provider(
        cases, ROOT / "evals/privacy/responses.fixture.json", "invalid_output"
    )
    report = await evaluate(
        cases,
        provider,
        fixture_profile(),
        model_digest="fixture-only",
        mode="fixture",
        runs=3,
        abstain_policy="accept",
        commit="a" * 40,
    )
    assert not report["passed"] and report["gates"]["invalid_output"]["count"] == 3


async def test_real_assembly_preserves_profile_text_only_cache_and_metadata() -> None:
    cases = load_cases(ROOT / "evals/privacy/cases.json", ROOT / "evals/privacy/expectations.json")
    fixture = fixture_provider(cases, ROOT / "evals/privacy/responses.fixture.json", "pass")
    payloads: list[dict[str, Any]] = []
    profiles = []

    class Recording(FixtureProvider):
        async def complete(self, profile: Any, payload: dict[str, Any]) -> dict[str, Any]:
            payloads.append(payload)
            profiles.append(profile)
            return await fixture.complete(profile, payload)

    # Wrap a fake with the exact real assembly, never instantiate a transport.
    provider = real_provider(Recording(fixture.addresses, fixture.responses, fixture.overrides))
    profile = fixture_profile().model_copy(
        update={"profile_id": "operator-copy", "timeout_seconds": 30, "token_counter": "llamacpp"}
    )
    report = await evaluate(
        cases,
        provider,
        profile,
        model_digest="synthetic-fixed-digest",
        mode="real",
        runs=3,
        abstain_policy="separate",
        commit="b" * 40,
        commit_dirty=True,
    )
    assert report["passed"] and report["quality_evidence"] is True and report["cache"] is False
    assert (
        report["model_digest"] == "synthetic-fixed-digest"
        and report["profile"]["token_counter"] == "llamacpp"
    )
    assert report["commit"] == {"sha": "b" * 40, "dirty": True}
    assert len(payloads) == 180 and all(p == profile for p in profiles)
    assert [json.loads(p["messages"][1]["content"]) for p in payloads] == [
        c.text for c in cases.cases.cases
    ] * 3
    for payload in payloads:
        assert payload["cache"] == {"no-cache": True, "no-store": True}
        assert payload["caching"] is False and payload["extra_body"] == {"cache_prompt": False}
        assert payload["max_completion_tokens"] == 256 and payload["stream"] is False
    assert profile.timeout_seconds == 30


def test_real_cli_with_fake_records_execution_conditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("privacy_cli", CLI)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cases = load_cases(ROOT / "evals/privacy/cases.json", ROOT / "evals/privacy/expectations.json")
    fake = fixture_provider(cases, ROOT / "evals/privacy/responses.fixture.json", "pass")
    monkeypatch.setattr(cli, "real_provider", lambda: real_provider(fake))
    config = json.loads((ROOT / "examples/characters.llamacpp.json").read_text(encoding="utf-8"))
    config[0]["card_path"] = str(ROOT / "characters/miori/miori.card.json")
    config[0]["profile"]["external_send_allowed"] = True
    path = tmp_path / "characters.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    output = tmp_path / "report.json"
    options = [
        "--mode",
        "real",
        "--characters",
        str(path),
        "--character-id",
        config[0]["character_id"],
        "--model-digest",
        "synthetic-sha",
        "--abstain-policy",
        "accept",
        "--runs",
        "3",
        "--output",
        str(output),
    ]
    assert cli.main(options) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["mode"] == "real" and report["model_digest"] == "synthetic-sha"
    assert report["profile"]["profile_id"] == config[0]["profile"]["profile_id"]
    for flag in ("--characters", "--character-id", "--model-digest", "--runs"):
        pos = options.index(flag)
        missing = options[:pos] + options[pos + 2 :]
        assert cli.main(missing) == 2
    pos = options.index("--abstain-policy")
    assert cli.main(options[:pos] + options[pos + 2 :]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["abstain_policy"] == "separate"
    assert "synthetic-sha" not in capsys.readouterr().err


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "invalid", "bad_scenario"])
def test_fixture_configuration_errors_do_not_disclose_content(fault: str, tmp_path: Path) -> None:
    fixture = json.loads(
        (ROOT / "evals/privacy/responses.fixture.json").read_text(encoding="utf-8")
    )
    first = next(iter(fixture["responses"]))
    if fault == "missing":
        del fixture["responses"][first]
    elif fault == "extra":
        fixture["responses"]["unknown"] = fixture["responses"][first]
    elif fault == "invalid":
        fixture["responses"][first]["classification"] = "private-response-marker"
    elif fault == "bad_scenario":
        fixture["scenarios"]["pass"]["unknown"] = {"action": "timeout"}
    path = tmp_path / "private-path-marker.json"
    raw = json.dumps(fixture)
    if fault == "duplicate":
        raw = raw[:-1] + ',"schema_version":1}'
    path.write_text(raw, encoding="utf-8")
    result = run("--abstain-policy", "accept", "--fixture", str(path))
    assert result.returncode == 2 and not result.stdout
    assert (
        "private-path-marker" not in result.stderr
        and "private-response-marker" not in result.stderr
    )


@pytest.mark.parametrize(
    "fault", ["transport", "permission", "capability", "digest", "cache_wrapper"]
)
async def test_real_invalid_configuration_stops_before_provider(fault: str) -> None:
    from digital_souls_core.application import Provider

    from .support import FakeProvider

    cases = load_cases(ROOT / "evals/privacy/cases.json", ROOT / "evals/privacy/expectations.json")
    fake = FakeProvider()
    provider: Provider = real_provider(fake)
    profile = fixture_profile()
    digest = "synthetic"
    if fault == "transport":
        profile = profile.model_copy(update={"transport": "sdk", "api_base": None})
    elif fault == "permission":
        profile = profile.model_copy(update={"external_send_allowed": False})
    elif fault == "capability":
        profile = profile.model_copy(update={"allowed_parameters": frozenset()})
    elif fault == "digest":
        digest = " "
    else:
        provider = fake
    with pytest.raises(ValueError):
        await evaluate(
            cases,
            provider,
            profile,
            model_digest=digest,
            mode="real",
            runs=3,
            abstain_policy="accept",
            commit="a" * 40,
        )
    assert fake.calls == []


def test_cli_default_abstain_policy_is_decision_c(tmp_path: Path) -> None:
    """ADR 0032 user decision C: separate is the default and 6/60 ABSTAIN fails."""
    output = tmp_path / "default.json"
    result = run("--runs", "3", "--output", str(output))
    assert result.returncode == 0
    assert json.loads(output.read_text(encoding="utf-8"))["abstain_policy"] == "separate"
    six = tmp_path / "six.json"
    result = run("--runs", "3", "--scenario", "abstain_6", "--output", str(six))
    report = json.loads(six.read_text(encoding="utf-8"))
    assert result.returncode == 1 and report["abstain_policy"] == "separate"
    assert all("abstain_rate: requires < 0.10" in r["fail_reasons"] for r in report["rounds"])
