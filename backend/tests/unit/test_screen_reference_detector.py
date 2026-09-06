import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.screen_perception.detector import (
    ScreenReferenceHistoryItem,
    decide_screen_reference,
    detect_screen_reference,
)


@pytest.mark.parametrize(
    "text",
    [
        "今の画面を見て説明して",
        "現在のスクリーンに何が表示されていますか？",
        "このウィンドウを確認して教えて",
        "画面の文字を読んで",
    ],
)
def test_explicit_natural_language_screen_requests_are_detected(text: str) -> None:
    assert detect_screen_reference(text).requested is True


@pytest.mark.parametrize(
    "text",
    [
        "これどう思う？",
        "画面は見ないで答えて",
        "「今の画面を見て」と言われた",
        "画面を見る機能について説明して",
        "昨日その画面を見た",
        "今日は静かだね",
    ],
)
def test_ambiguous_negated_quoted_and_unrelated_text_does_not_trigger(
    text: str,
) -> None:
    assert detect_screen_reference(text).requested is False


def test_detector_normalizes_full_width_and_whitespace_deterministically() -> None:
    decision = detect_screen_reference("  今の　画面を\n確認して！ ")

    assert decision.requested is True
    assert decision.normalized_text == "今の 画面を 確認して!"


class _FixtureJudge:
    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.estimates: list[dict[str, object]] = []
        self.generations: list[dict[str, object]] = []

    def estimate_input_tokens(self, **request: object) -> object:
        self.estimates.append(request)
        return SimpleNamespace(count=120)

    def generate_structured(self, **request: object) -> object:
        self.generations.append(request)
        basis = (
            "competing_references"
            if self.decision == "clarify_reference"
            else "current_message"
        )
        return SimpleNamespace(value={"decision": self.decision, "basis": basis})


def test_contextual_reference_fixture_is_decided_without_unauthorized_capture() -> None:
    fixture_path = (
        Path(__file__).parents[3]
        / "contracts/perception/screen/fixtures/contextual-reference-cases.json"
    )
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))["cases"]

    for case in cases:
        judge = _FixtureJudge(case["expected_decision"])
        history = tuple(
            ScreenReferenceHistoryItem(
                item["role"], item["content"], item["screen_provenance"]
            )
            for item in case["history"]
        )
        decision = decide_screen_reference(
            case["current_message"],
            sharing_active=case["sharing"] == "active",
            screen_use_authorized=case["screen_use_authorized"],
            explicit_ui=case["explicit_ui"],
            history=history,
            router=judge,
            cloud_judge_history_allowed=(
                case.get("reference_judge_destination") != "cloud"
                or all(
                    item.screen_provenance != "expired_session" for item in history
                )
            ),
        )

        assert decision.decision == case["expected_decision"], case["id"]
        assert decision.path == case["expected_path"], case["id"]
        if case["expected_path"] == "llm":
            assert len(judge.generations) == 1
            assert judge.generations[0]["max_input_tokens"] == 1536
            assert judge.generations[0]["max_output_tokens"] == 64
            assert judge.generations[0]["timeout_seconds"] == 3.0
        else:
            assert judge.generations == []


def test_competing_reference_judge_failure_never_requests_an_image() -> None:
    class FailingJudge(_FixtureJudge):
        def generate_structured(self, **request: object) -> object:
            raise ValueError("synthetic invalid result")

    decision = decide_screen_reference(
        "貼ったエラーと今の画面、これはどっち？",
        sharing_active=True,
        screen_use_authorized=True,
        history=(ScreenReferenceHistoryItem("user", "Error: synthetic"),),
        router=FailingJudge("inspect_screen"),
    )

    assert decision.decision == "clarify_reference"
    assert decision.path == "fallback"


def test_unavailable_screen_candidate_is_distinguished_from_prohibition() -> None:
    unavailable = decide_screen_reference(
        "これ何？",
        sharing_active=False,
        screen_use_authorized=False,
    )
    prohibited = decide_screen_reference(
        "画面は見ないで。これ何？",
        sharing_active=False,
        screen_use_authorized=False,
    )

    assert unavailable.decision == "answer_without_screen"
    assert unavailable.screen_candidate is True
    assert prohibited.screen_candidate is False


def test_unexpected_reference_judge_failure_uses_safe_fallback() -> None:
    class UnexpectedFailureJudge(_FixtureJudge):
        def generate_structured(self, **request: object) -> object:
            raise RuntimeError("synthetic provider failure")

    decision = decide_screen_reference(
        "貼ったエラーと今の画面、これはどっち？",
        sharing_active=True,
        screen_use_authorized=True,
        history=(ScreenReferenceHistoryItem("user", "Error: synthetic"),),
        router=UnexpectedFailureJudge("inspect_screen"),
    )

    assert decision.decision == "clarify_reference"
    assert decision.path == "fallback"
