_REQUIRED_RESPONSE_FIELDS = (
    "classification",
    "subject_scope",
    "category",
    "reason_code",
)
_SENSITIVE_CATEGORIES = (
    "HEALTH",
    "MENTAL_STATE",
    "SELF_HARM",
    "ABUSE_OR_SEXUAL_VIOLENCE",
    "FINANCIAL_SITUATION",
    "THIRD_PARTY_PRIVATE",
    "OTHER_SENSITIVE",
)


def _response_branch(
    classification: str,
    subject_scopes: tuple[str, ...],
    categories: tuple[str, ...],
    reason_code: str,
) -> dict[str, object]:
    """モデル出力をPrivacyAssessmentの整合条件へ制約する。"""
    return {
        "type": "object",
        "properties": {
            "classification": {"enum": [classification]},
            "subject_scope": {"enum": list(subject_scopes)},
            "category": {"enum": list(categories)},
            "reason_code": {"enum": [reason_code]},
        },
        "required": list(_REQUIRED_RESPONSE_FIELDS),
        "additionalProperties": False,
    }


SEMANTIC_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "oneOf": [
        _response_branch(
            "SENSITIVE",
            ("SELF", "THIRD_PARTY"),
            _SENSITIVE_CATEGORIES,
            "SENSITIVE_CONTENT",
        ),
        _response_branch(
            "NOT_SENSITIVE",
            ("SELF", "THIRD_PARTY", "GENERAL"),
            ("NONE",),
            "NO_SENSITIVE_CONTENT",
        ),
        _response_branch(
            "ABSTAIN",
            ("UNKNOWN",),
            ("UNKNOWN",),
            "UNKNOWN_LANGUAGE",
        ),
    ],
}
