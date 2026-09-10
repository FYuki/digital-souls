from app.memory.admission.contracts import (
    EpisodicEventValue,
    InteractionAspect,
    InteractionPreferenceValue,
    PreferencePolarity,
    StructuredValue,
    UserPreferenceValue,
)

from app.memory.episode import render_episode

USER_PREFERENCE_TEMPLATES = {
    PreferencePolarity.LIKE: "ユーザーは{object}を好む。",
    PreferencePolarity.DISLIKE: "ユーザーは{object}を好まない。",
    PreferencePolarity.PREFER_OVER: "ユーザーは{alternative}より{object}を好む。",
}
INTERACTION_PREFERENCE_TEMPLATES = {
    InteractionAspect.ADDRESSING: "ユーザーは{value}と呼ばれることを望む。",
    InteractionAspect.TONE: "ユーザーは{value}な話し方を望む。",
    InteractionAspect.RESPONSE_FORMAT: "ユーザーは回答を{value}で受け取ることを望む。",
    InteractionAspect.RESPONSE_LENGTH: "ユーザーは{value}の回答を望む。",
    InteractionAspect.LANGUAGE: "ユーザーは{value}での会話を望む。",
}


def render_normalized_text(value: StructuredValue) -> str:
    if isinstance(value, EpisodicEventValue):
        return render_episode(value)
    if isinstance(value, UserPreferenceValue):
        return USER_PREFERENCE_TEMPLATES[value.polarity].format(
            object=value.object,
            alternative=value.alternative,
        )
    if isinstance(value, InteractionPreferenceValue):
        return INTERACTION_PREFERENCE_TEMPLATES[value.aspect].format(value=value.value)
    raise TypeError("value must be an allowlist structured value")
