import importlib

import pytest


def _contracts():
    return importlib.import_module("app.memory.admission.contracts")


def _render_normalized_text(value):
    templates = importlib.import_module("app.memory.admission.templates")
    return templates.render_normalized_text(value)


@pytest.mark.parametrize(
    ("subject_name", "prefix"),
    [
        ("USER", "ユーザーが"),
        ("SHARED", "ユーザーと"),
    ],
)
@pytest.mark.parametrize(
    ("event_type_name", "predicate"),
    [
        ("SHARED_MILESTONE", "という節目を迎えた。"),
        ("ACHIEVEMENT", "を達成した。"),
        ("DECISION", "を決めた。"),
        ("OUTCOME", "という結果になった。"),
        ("CHANGE", "という変化があった。"),
    ],
)
def test_renders_every_episodic_subject_and_event_combination(
    subject_name: str,
    prefix: str,
    event_type_name: str,
    predicate: str,
) -> None:
    contracts = _contracts()
    value = contracts.EpisodicEventValue(
        contracts.EpisodicEventType[event_type_name],
        contracts.EpisodicSubject[subject_name],
        "資格取得",
    )

    assert _render_normalized_text(value) == f"{prefix}資格取得{predicate}"


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
