from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


def _response_audio_segments(*texts: str) -> tuple[str, list[dict[str, object]]]:
    generated_text = "".join(texts)
    segments: list[dict[str, object]] = []
    start = 0
    for sequence, text in enumerate(texts, start=1):
        end = start + len(text)
        segments.append(
            {
                "audio_sequence": sequence,
                "text_range": {"start": start, "end": end},
            }
        )
        start = end
    return generated_text, segments


def test_should_resume_the_client_selected_character_conversation_boundary() -> None:
    repository = MagicMock()
    repository.resume_conversation.return_value = SimpleNamespace(
        character_id="miori",
        conversation_id=CONVERSATION_ID,
    )
    service = ConversationHistoryService(repository, MagicMock())

    session = service.open_session("miori", CONVERSATION_ID)

    repository.resume_conversation.assert_called_once_with("miori", CONVERSATION_ID)
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

    persisted_turn = session.complete_turn(started, "完全な回答")

    sanitizer.sanitize_assistant.assert_called_once_with("完全な回答")
    repository.complete_turn.assert_not_called()
    repository.skip_processing_turn_for_privacy.assert_not_called()
    assert persisted_turn is repository.create_privacy_skipped_turn.return_value


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

    persisted_turn = session.complete_turn(started, raw_assistant_secret)

    persisted = repository.skip_processing_turn_for_privacy.call_args.args[3]
    assert persisted.reason_code is HistoryDecisionReasonCode.SCAN_FAILURE
    assert persisted.sanitizer_version == SANITIZER_VERSION
    assert persisted.policy_version == POLICY_VERSION
    assert raw_assistant_secret not in repr(repository.mock_calls)
    repository.complete_turn.assert_not_called()
    assert persisted_turn is repository.skip_processing_turn_for_privacy.return_value


def test_should_sanitize_interrupted_partial_reply_before_persistence() -> None:
    generated_text, response_audio_segments = _response_audio_segments(
        "再生",
        "済み",
        " token=raw-assistant-secret-94",
        " 未再生",
        " suffix",
    )
    raw_partial = "再生済み token=raw-assistant-secret-94"
    masked_partial = "再生済み token=[TOKEN]"
    repository = MagicMock()
    sanitizer = MagicMock()
    sanitizer.sanitize_assistant.return_value = _decision(
        ConversationHistoryAction.STORE_MASKED,
        HistoryDecisionReasonCode.MASKED,
        masked_partial,
    )
    session = _session(repository, sanitizer)
    started = SimpleNamespace(turn_id=TURN_ID, content_skipped=False)

    persisted_turn = session.interrupt_turn(
        started,
        generated_text,
        response_audio_segments,
        3,
    )

    sanitizer.sanitize_assistant.assert_called_once_with(raw_partial)
    repository.interrupt_turn.assert_called_once_with(
        "miori",
        CONVERSATION_ID,
        TURN_ID,
        sanitized_assistant_content=masked_partial,
    )
    assert raw_partial not in repr(repository.mock_calls)
    assert persisted_turn is repository.interrupt_turn.return_value


def test_should_persist_empty_interrupted_reply_when_no_chunk_was_played() -> None:
    generated_text, response_audio_segments = _response_audio_segments(
        "再生されていない",
        "回答",
    )
    repository = MagicMock()
    sanitizer = MagicMock()
    sanitizer.sanitize_assistant.return_value = _decision(
        ConversationHistoryAction.STORE_MASKED,
        HistoryDecisionReasonCode.UNCHANGED,
        "",
    )
    session = _session(repository, sanitizer)
    started = SimpleNamespace(turn_id=TURN_ID, content_skipped=False)

    session.interrupt_turn(
        started,
        generated_text,
        response_audio_segments,
        0,
    )

    sanitizer.sanitize_assistant.assert_called_once_with("")
    repository.interrupt_turn.assert_called_once_with(
        "miori",
        CONVERSATION_ID,
        TURN_ID,
        sanitized_assistant_content="",
    )


def test_should_reject_invalid_playback_range_before_sanitization_or_persistence() -> None:
    generated_text, response_audio_segments = _response_audio_segments("生成済み回答")
    repository = MagicMock()
    sanitizer = MagicMock()
    session = _session(repository, sanitizer)
    started = SimpleNamespace(turn_id=TURN_ID, content_skipped=False)

    with pytest.raises(ValueError, match="last_played_audio_sequence must not be negative"):
        session.interrupt_turn(
            started,
            generated_text,
            response_audio_segments,
            -1,
        )

    sanitizer.sanitize_assistant.assert_not_called()
    repository.interrupt_turn.assert_not_called()
    repository.skip_processing_turn_for_privacy.assert_not_called()


def test_should_erase_processing_content_when_interrupted_partial_scan_fails() -> None:
    generated_text, response_audio_segments = _response_audio_segments(
        "保存禁止の",
        "partial",
        "だけが再生済み",
        "未再生の本文",
        "とsuffix",
    )
    repository = MagicMock()
    sanitizer = MagicMock()
    sanitizer.sanitize_assistant.return_value = _decision(
        ConversationHistoryAction.SKIP_CONTENT,
        HistoryDecisionReasonCode.SCAN_FAILURE,
        None,
    )
    session = _session(repository, sanitizer)
    started = SimpleNamespace(turn_id=TURN_ID, content_skipped=False)

    persisted_turn = session.interrupt_turn(
        started,
        generated_text,
        response_audio_segments,
        3,
    )

    sanitizer.sanitize_assistant.assert_called_once_with("保存禁止のpartialだけが再生済み")
    persisted = repository.skip_processing_turn_for_privacy.call_args.args[3]
    assert persisted.reason_code is HistoryDecisionReasonCode.SCAN_FAILURE
    repository.interrupt_turn.assert_not_called()
    assert persisted_turn is repository.skip_processing_turn_for_privacy.return_value
