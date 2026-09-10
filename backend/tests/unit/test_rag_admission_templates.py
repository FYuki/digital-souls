import importlib

import pytest


def _contracts():
    return importlib.import_module("app.memory.admission.contracts")


def _render_normalized_text(value):
    templates = importlib.import_module("app.memory.admission.templates")
    return templates.render_normalized_text(value)


def test_renders_an_episode_from_its_owner_and_structured_action() -> None:
    contracts = _contracts()
    value = contracts.EpisodicEventValue(
        contracts.EpisodicEventType.ACTIVITY,
        "miori",
        "光織",
        "静岡への旅行",
        "静岡へ行った",
    )
    assert _render_normalized_text(value) == "光織は静岡へ行った。"


@pytest.mark.parametrize(
    ("polarity_name", "object_value", "alternative", "expected"),
    [
        ("LIKE", "コーヒー", None, "ユーザーはコーヒーを好む。"),
        ("DISLIKE", "コーヒー", None, "ユーザーはコーヒーを好まない。"),
        ("PREFER_OVER", "コーヒー", "紅茶", "ユーザーは紅茶よりコーヒーを好む。"),
    ],
)
def test_renders_every_user_preference_template(
    polarity_name: str,
    object_value: str,
    alternative: str | None,
    expected: str,
) -> None:
    contracts = _contracts()
    value = contracts.UserPreferenceValue(
        contracts.PreferencePolarity[polarity_name],
        object_value,
        alternative=alternative,
    )

    assert _render_normalized_text(value) == expected


@pytest.mark.parametrize(
    ("aspect_name", "value", "expected"),
    [
        (
            "ADDRESSING",
            "ミオリ",
            "ユーザーはミオリと呼ばれることを望む。",
        ),
        (
            "TONE",
            "穏やか",
            "ユーザーは穏やかな話し方を望む。",
        ),
        (
            "RESPONSE_FORMAT",
            "箇条書き",
            "ユーザーは回答を箇条書きで受け取ることを望む。",
        ),
        (
            "RESPONSE_LENGTH",
            "短め",
            "ユーザーは短めの回答を望む。",
        ),
        (
            "LANGUAGE",
            "日本語",
            "ユーザーは日本語での会話を望む。",
        ),
    ],
)
def test_renders_every_interaction_preference_template(
    aspect_name: str,
    value: str,
    expected: str,
) -> None:
    contracts = _contracts()
    structured_value = contracts.InteractionPreferenceValue(
        contracts.InteractionAspect[aspect_name], value
    )

    assert _render_normalized_text(structured_value) == expected
