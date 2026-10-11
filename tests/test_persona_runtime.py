"""Both production coordinators exercised with fixture-only adapters."""

from pathlib import Path

import pytest

from digital_souls_core.character import load_characters
from digital_souls_core.persona_evaluation import FakeJudge, load_cases
from digital_souls_core.persona_fixtures import fixture_entries
from digital_souls_core.persona_runtime import evaluate

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.it1


@pytest.mark.parametrize(
    "scenario",
    ["pass", "fail", "persona_break", "override_followed", "mismatch", "card_hash_mismatch"],
)
async def test_fixture_scenarios_and_real_routing(scenario: str) -> None:
    cases = load_cases(ROOT / "evals/persona/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    entries, providers = fixture_entries(
        char, cases, ROOT / "evals/persona/responses.fixture.json", scenario
    )
    result = await evaluate(cases, entries, judge=FakeJudge(), commit="a" * 40)
    # Complete baseline explicitly fails because production history rejects system.
    assert result["status"] == "FAIL"
    assert result["gates"]["execution_incomplete"]["count"] == 3
    assert providers["chat"].calls == 12
    assert providers["history"].calls == 9
    assert all(
        row["card_hashes"]
        for row in result["summaries"]
        if row["category"] != "override_resistance" or row["entry"] == "chat"
    )
    if scenario in ("persona_break", "override_followed", "card_hash_mismatch"):
        assert result["gates"][scenario]["count"] > 0
    assert result["egogram"]["entries"]["chat"]["tendencies"] == dict.fromkeys(
        ("CP", "NP", "A", "FC", "AC"), "fixture: unestimated"
    )
    assert all(
        "Luma" in payload["messages"][0]["content"] for payload in providers["chat"].payloads
    )
    assert "Commander" in providers["chat"].payloads[-1]["messages"][1]["content"]
    history_likes = providers["history"].payloads[1]["messages"]
    assert history_likes[-3:] == [m.model_dump() for m in cases.cases[1].messages]
    assert history_likes[0]["role"] == "system" and "Luma" in history_likes[0]["content"]


async def test_missing_required_entry_cannot_pass() -> None:
    cases = load_cases(ROOT / "evals/persona/synthetic.cases.json")
    char = load_characters(ROOT / "evals/persona/synthetic.characters.json")[0]
    entries, _ = fixture_entries(char, cases, ROOT / "evals/persona/responses.fixture.json", "pass")
    with pytest.raises(ValueError, match="empty evaluation"):
        await evaluate(cases, {"chat": entries["chat"]}, judge=FakeJudge(), commit="a" * 40)
