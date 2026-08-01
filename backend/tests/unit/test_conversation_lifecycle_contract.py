from types import SimpleNamespace
from unittest.mock import MagicMock

from app.conversation_history.service import (
    ConversationHistoryService,
    ConversationHistorySession,
)
from app.privacy.contracts import (
    ConversationHistoryAction,
    ConversationHistoryDecision,
    HistoryDecisionReasonCode,
)
from tests.conversation_history_test_support import CONVERSATION_ID, TURN_ID


SANITIZER_VERSION = "test-sanitizer-v2"
POLICY_VERSION = "test-policy-v3"


def _decision(
    action: ConversationHistoryAction,
    reason_code: HistoryDecisionReasonCode,
    content: str | None,
) -> ConversationHistoryDecision:
    return ConversationHistoryDecision(
        action=action,
        reason_code=reason_code,
        sanitizer_version=SANITIZER_VERSION,
        policy_version=POLICY_VERSION,
        content=content,
    )


def _session(repository: MagicMock, sanitizer: MagicMock) -> ConversationHistorySession:
    return ConversationHistorySession(
        character_id="miori",
        conversation_id=CONVERSATION_ID,
        repository=repository,
        sanitizer=sanitizer,
    )


def test_should_open_the_client_selected_character_conversation_boundary() -> None:
    repository = MagicMock()
    repository.ensure_conversation.return_value = SimpleNamespace(
        character_id="miori",
        conversation_id=CONVERSATION_ID,
    )
    service = ConversationHistoryService(repository, MagicMock())

    session = service.open_session("miori", CONVERSATION_ID)

    repository.ensure_conversation.assert_called_once_with("miori", CONVERSATION_ID)
    assert isinstance(session, ConversationHistorySession)


def test_should_pass_only_masked_user_content_to_processing_persistence() -> None:
    raw_secret = "password: raw-secret-91"
    masked = "password: [PASSWORD]"
    repository = MagicMock()
    repository.create_processing_turn.return_value = SimpleNamespace(turn_id=TURN_ID)
    sanitizer = MagicMock()
    sanitizer.sanitize_current_user.return_value = _decision(
        ConversationHistoryAction.STORE_MASKED,
        HistoryDecisionReasonCode.MASKED,
        masked,
    )

    started = _session(repository, sanitizer).start_turn(raw_secret)

    sanitizer.sanitize_current_user.assert_called_once_with(raw_secret)
    persisted = repository.create_processing_turn.call_args.args[2]
    assert persisted.sanitized_user_content == masked
    assert raw_secret not in repr(repository.mock_calls)
    assert started.turn_id == TURN_ID
    assert started.content_skipped is False


def test_should_persist_user_scan_failure_as_metadata_only_without_reason_conversion() -> None:
    raw_secret = "password: raw-secret-92"
    repository = MagicMock()
    repository.create_privacy_skipped_turn.return_value = SimpleNamespace(turn_id=TURN_ID)
    sanitizer = MagicMock()
    sanitizer.sanitize_current_user.return_value = _decision(
        ConversationHistoryAction.SKIP_CONTENT,
        HistoryDecisionReasonCode.SCAN_FAILURE,
        None,
    )

    started = _session(repository, sanitizer).start_turn(raw_secret)

    persisted = repository.create_privacy_skipped_turn.call_args.args[2]
    assert persisted.reason_code is HistoryDecisionReasonCode.SCAN_FAILURE
    assert persisted.sanitizer_version == SANITIZER_VERSION
    assert persisted.policy_version == POLICY_VERSION
    assert set(vars(persisted)) == {
        "reason_code",
        "sanitizer_version",
        "policy_version",
    }
    assert raw_secret not in repr(repository.mock_calls)
    assert started.content_skipped is True


def test_should_sanitize_complete_assistant_reply_even_after_user_content_skip() -> None:
    repository = MagicMock()
    repository.create_privacy_skipped_turn.return_value = SimpleNamespace(turn_id=TURN_ID)
    sanitizer = MagicMock()
    sanitizer.sanitize_current_user.return_value = _decision(
        ConversationHistoryAction.SKIP_CONTENT,
        HistoryDecisionReasonCode.STORAGE_OPT_OUT,
        None,
    )
    sanitizer.sanitize_assistant.return_value = _decision(
        ConversationHistoryAction.STORE_MASKED,
        HistoryDecisionReasonCode.UNCHANGED,
        "完全な回答",
    )
    session = _session(repository, sanitizer)
    started = session.start_turn("このターンは履歴に残さないで")

    delivery_trackable = session.complete_turn(started, "完全な回答")

    sanitizer.sanitize_assistant.assert_called_once_with("完全な回答")
    repository.complete_turn.assert_not_called()
    repository.skip_processing_turn_for_privacy.assert_not_called()
    assert delivery_trackable is False


def test_should_atomically_erase_processing_content_on_assistant_scan_failure() -> None:
    raw_assistant_secret = "token=raw-assistant-secret-93"
    repository = MagicMock()
    sanitizer = MagicMock()
    sanitizer.sanitize_assistant.return_value = _decision(
        ConversationHistoryAction.SKIP_CONTENT,
        HistoryDecisionReasonCode.SCAN_FAILURE,
        None,
    )
    session = _session(repository, sanitizer)
    started = SimpleNamespace(turn_id=TURN_ID, content_skipped=False)

    delivery_trackable = session.complete_turn(started, raw_assistant_secret)

    persisted = repository.skip_processing_turn_for_privacy.call_args.args[3]
    assert persisted.reason_code is HistoryDecisionReasonCode.SCAN_FAILURE
    assert persisted.sanitizer_version == SANITIZER_VERSION
    assert persisted.policy_version == POLICY_VERSION
    assert raw_assistant_secret not in repr(repository.mock_calls)
    repository.complete_turn.assert_not_called()
    assert delivery_trackable is False
