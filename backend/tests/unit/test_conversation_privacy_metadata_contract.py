import sqlite3
from pathlib import Path

from app.conversation_history.models import (
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
    TurnStatus,
)
from app.privacy.contracts import HistoryDecisionReasonCode
from tests.conversation_history_test_support import CONVERSATION_ID, create_repository


SANITIZER_VERSION = "history-sanitizer-v2"
POLICY_VERSION = "privacy-policy-v3"


def _privacy_input() -> PrivacySkippedTurnInput:
    return PrivacySkippedTurnInput(
        reason_code=HistoryDecisionReasonCode.SCAN_FAILURE,
        sanitizer_version=SANITIZER_VERSION,
        policy_version=POLICY_VERSION,
    )


def test_should_roundtrip_privacy_skip_reason_and_versions_without_content(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    repository = create_repository(database_path)
    repository.create_conversation("miori")

    created = repository.create_privacy_skipped_turn(
        "miori",
        CONVERSATION_ID,
        _privacy_input(),
    )
    restored = repository.list_turns("miori", CONVERSATION_ID)[0]

    assert created == restored
    assert restored.status is TurnStatus.PRIVACY_SKIPPED
    assert restored.user_content is None
    assert restored.assistant_content is None
    assert restored.privacy_reason_code is HistoryDecisionReasonCode.SCAN_FAILURE
    assert restored.sanitizer_version == SANITIZER_VERSION
    assert restored.policy_version == POLICY_VERSION


def test_should_store_assistant_skip_and_user_erasure_in_one_committed_state(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    repository = create_repository(database_path)
    repository.create_conversation("miori")

    processing = repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="保存済みのマスク済み本文"),
    )

    skipped = repository.skip_processing_turn_for_privacy(
        "miori",
        CONVERSATION_ID,
        processing.turn_id,
        _privacy_input(),
    )

    assert skipped.status is TurnStatus.PRIVACY_SKIPPED
    assert skipped.user_content is None
    assert skipped.assistant_content is None
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT user_content, assistant_content, status, "
            "privacy_reason_code, sanitizer_version, policy_version "
            "FROM conversation_turns WHERE turn_id = ?",
            (str(processing.turn_id),),
        ).fetchone()
    assert row == (
        None,
        None,
        "privacy_skipped",
        "SCAN_FAILURE",
        SANITIZER_VERSION,
        POLICY_VERSION,
    )
