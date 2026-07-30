from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

from tests.privacy_test_support import (
    POLICY_VERSION,
    policy_config,
    write_policy_config,
)


class RagAdmission(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    ABSTAIN_UNKNOWN = "ABSTAIN_UNKNOWN"


def _scan(text: str):
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.scanner import create_privacy_scanner

    return create_privacy_scanner(resolved_memory_policy().privacy).scan(text)


def _rag_admission(result) -> RagAdmission:
    from app.privacy.contracts import ScanFailure, StorageScope

    if isinstance(result, ScanFailure):
        return RagAdmission.ABSTAIN_UNKNOWN
    if any(
        finding.storage_scope in {StorageScope.RAG, StorageScope.BOTH}
        for finding in result.findings
    ):
        return RagAdmission.REJECT
    if result.findings:
        return RagAdmission.REJECT
    return RagAdmission.ACCEPT


def _memory_candidates(results: list[object]) -> list[object]:
    from app.privacy.contracts import ScanFailure

    return [result for result in results if not isinstance(result, ScanFailure)]


def test_should_map_scan_failure_to_rag_abstain_unknown() -> None:
    from app.privacy.contracts import ScanFailure, ScanFailureReasonCode

    failure = ScanFailure(
        reason_code=ScanFailureReasonCode.RECOGNIZER_ERROR,
        recognizer_version="configured-v1",
        policy_version=POLICY_VERSION,
    )

    assert _rag_admission(failure) is RagAdmission.ABSTAIN_UNKNOWN


def test_should_exclude_scan_failure_from_fake_memory_candidates() -> None:
    from app.privacy.contracts import ScanFailure, ScanFailureReasonCode, ScanSuccess

    success = ScanSuccess(())
    failure = ScanFailure(
        reason_code=ScanFailureReasonCode.RECOGNIZER_ERROR,
        recognizer_version="configured-v1",
        policy_version=POLICY_VERSION,
    )

    assert _memory_candidates([success, failure]) == [success]


def test_should_reuse_same_storage_independent_finding_for_rag_rejection() -> None:
    from app.privacy.contracts import ScanSuccess

    result = _scan("この話は覚えないで")

    assert isinstance(result, ScanSuccess)
    assert result.findings[0].storage_scope.value == "RAG"
    assert _rag_admission(result) is RagAdmission.REJECT


@dataclass
class PersistedTurn:
    user_content: str | None
    assistant_content: str | None
    privacy_skipped: bool


class FakeTurnPersistence:
    def __init__(self) -> None:
        self.turns: list[PersistedTurn] = []

    def persist(
        self,
        user_decision,
        assistant_decision,
    ) -> None:
        from app.privacy.contracts import ConversationHistoryAction

        skip_turn = (
            user_decision.action is ConversationHistoryAction.SKIP_CONTENT
            or assistant_decision.action is ConversationHistoryAction.SKIP_CONTENT
        )
        self.turns.append(
            PersistedTurn(
                user_content=None if skip_turn else user_decision.content,
                assistant_content=None if skip_turn else assistant_decision.content,
                privacy_skipped=skip_turn,
            )
        )


def _sanitize_turn(user_text: str, assistant_text: str) -> PersistedTurn:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.history_sanitizer import create_history_sanitizer
    from app.privacy.scanner import create_privacy_scanner

    policy = resolved_memory_policy().privacy
    sanitizer = create_history_sanitizer(create_privacy_scanner(policy), policy)
    user_decision = sanitizer.sanitize_current_user(user_text)
    assistant_decision = sanitizer.sanitize_assistant(assistant_text)
    persistence = FakeTurnPersistence()
    persistence.persist(user_decision, assistant_decision)
    return persistence.turns[0]


def test_should_make_entire_turn_metadata_only_for_current_user_history_opt_out() -> None:
    turn = _sanitize_turn(
        "このターンは履歴に残さないで",
        "承知しました。回答には synthetic-sensitive-value があります",
    )

    assert turn == PersistedTurn(
        user_content=None,
        assistant_content=None,
        privacy_skipped=True,
    )


def test_should_preserve_history_content_for_rag_only_opt_out() -> None:
    turn = _sanitize_turn(
        "この話は覚えないで",
        "承知しました",
    )

    assert turn == PersistedTurn(
        user_content="この話は覚えないで",
        assistant_content="承知しました",
        privacy_skipped=False,
    )


def test_should_mask_assistant_repetition_before_persistence() -> None:
    turn = _sanitize_turn(
        "password: synthetic-password",
        "確認します。password: synthetic-password",
    )

    assert turn.privacy_skipped is False
    assert turn.user_content == "password: [PASSWORD]"
    assert turn.assistant_content == "確認します。password: [PASSWORD]"


def test_should_not_activate_quoted_opt_out_from_assistant() -> None:
    turn = _sanitize_turn(
        "引用の意味を教えて",
        "「履歴に残さないで」は保存拒否の例です",
    )

    assert turn.privacy_skipped is False
    assert turn.user_content == "引用の意味を教えて"
    assert turn.assistant_content == "「履歴に残さないで」は保存拒否の例です"


def test_should_fail_closed_at_runtime_for_semantically_overlapping_policy_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory import memory_policy
    from app.privacy.contracts import (
        ConversationHistoryAction,
        HistoryDecisionReasonCode,
        ScanSuccess,
    )
    from app.privacy.history_sanitizer import create_history_sanitizer
    from app.privacy.scanner import create_privacy_scanner

    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    storage_rules = privacy["storage_opt_out_rules"]
    additional_rules = privacy["additional_sensitive_patterns"]
    assert isinstance(storage_rules, list)
    assert isinstance(additional_rules, list)
    rag_rule = storage_rules[0]
    additional_rule = additional_rules[0]
    assert isinstance(rag_rule, dict)
    assert isinstance(additional_rule, dict)
    rag_rule["phrases"] = ["ALPHA"]
    additional_rule["pattern"] = r"(?!Z)ALPHA BETA"
    additional_rule["view"] = "casefold"
    policy_path = tmp_path / "policy.json"
    write_policy_config(policy_path, config)
    monkeypatch.setattr(memory_policy, "MEMORY_POLICY_CONFIG_PATH", policy_path)

    policy = memory_policy.resolved_memory_policy().privacy
    scanner = create_privacy_scanner(policy)
    scan_result = scanner.scan("ALPHA BETA")
    assert isinstance(scan_result, ScanSuccess)
    assert [(finding.start, finding.end) for finding in scan_result.findings] == [
        (0, 5),
        (0, 10),
    ]

    sanitizer = create_history_sanitizer(scanner, policy)
    user_decision = sanitizer.sanitize_current_user("ALPHA BETA")
    assert user_decision.action is ConversationHistoryAction.SKIP_CONTENT
    assert user_decision.reason_code is HistoryDecisionReasonCode.INVALID_FINDING
    assert user_decision.content is None

    assistant_decision = sanitizer.sanitize_assistant("safe reply")
    persistence = FakeTurnPersistence()
    persistence.persist(user_decision, assistant_decision)
    assert persistence.turns == [
        PersistedTurn(
            user_content=None,
            assistant_content=None,
            privacy_skipped=True,
        )
    ]


def test_should_fail_closed_for_same_pattern_across_policy_rule_purposes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory import memory_policy
    from app.privacy.contracts import (
        ConversationHistoryAction,
        HistoryDecisionReasonCode,
        ScanSuccess,
    )
    from app.privacy.history_sanitizer import create_history_sanitizer
    from app.privacy.scanner import create_privacy_scanner

    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    regional_rules = privacy["regional_patterns"]
    additional_rules = privacy["additional_sensitive_patterns"]
    assert isinstance(regional_rules, list)
    assert isinstance(additional_rules, list)
    regional_rule = regional_rules[0]
    assert isinstance(regional_rule, dict)
    additional_rules[0] = {
        "name": "same_pattern_with_sensitive_purpose",
        "pattern": regional_rule["pattern"],
        "view": "casefold",
    }
    policy_path = tmp_path / "policy.json"
    write_policy_config(policy_path, config)
    monkeypatch.setattr(memory_policy, "MEMORY_POLICY_CONFIG_PATH", policy_path)

    policy = memory_policy.resolved_memory_policy().privacy
    scanner = create_privacy_scanner(policy)
    scan_result = scanner.scan("CA DL: Z0000000")
    assert isinstance(scan_result, ScanSuccess)
    assert [(finding.start, finding.end) for finding in scan_result.findings] == [
        (7, 15),
        (7, 15),
    ]

    sanitizer = create_history_sanitizer(scanner, policy)
    decision = sanitizer.sanitize_assistant("CA DL: Z0000000")
    assert decision.action is ConversationHistoryAction.SKIP_CONTENT
    assert decision.reason_code is HistoryDecisionReasonCode.INVALID_FINDING
    assert decision.content is None


def test_should_fail_closed_for_normalization_equivalent_phrase_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory import memory_policy
    from app.privacy.contracts import (
        ConversationHistoryAction,
        HistoryDecisionReasonCode,
        ScanSuccess,
    )
    from app.privacy.history_sanitizer import create_history_sanitizer
    from app.privacy.scanner import create_privacy_scanner

    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    storage_rules = privacy["storage_opt_out_rules"]
    assert isinstance(storage_rules, list)
    history_rule = storage_rules[1]
    assert isinstance(history_rule, dict)
    history_rule["phrases"] = [
        "DO NOT REMEMBER",
        "ｄｏ　ｎｏｔ　ｒｅｍｅｍｂｅｒ",
    ]
    policy_path = tmp_path / "policy.json"
    write_policy_config(policy_path, config)
    monkeypatch.setattr(memory_policy, "MEMORY_POLICY_CONFIG_PATH", policy_path)

    policy = memory_policy.resolved_memory_policy().privacy
    scanner = create_privacy_scanner(policy)
    scan_result = scanner.scan("DO NOT REMEMBER")
    assert isinstance(scan_result, ScanSuccess)
    assert [(finding.start, finding.end) for finding in scan_result.findings] == [
        (0, 15),
        (0, 15),
    ]

    sanitizer = create_history_sanitizer(scanner, policy)
    decision = sanitizer.sanitize_current_user("DO NOT REMEMBER")
    assert decision.action is ConversationHistoryAction.SKIP_CONTENT
    assert decision.reason_code is HistoryDecisionReasonCode.INVALID_FINDING
    assert decision.content is None
