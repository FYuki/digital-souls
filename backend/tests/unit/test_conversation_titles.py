from pathlib import Path

import pytest

from app.conversation_history.models import (
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
)
from app.conversation_history.titles import (
    DEFAULT_CONVERSATION_TITLE,
    generate_conversation_title,
    normalize_manual_conversation_title,
)
from app.privacy.contracts import HistoryDecisionReasonCode
from tests.conversation_history_test_support import create_repository


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("  最初の質問です。 続きです。 ", "最初の質問です。"),
        ("改行を\n含む\t質問", "改行を 含む 質問"),
        ("Cafe\u0301について", "Caféについて"),
        ("a" * 40, "a" * 40),
        ("a" * 41, "a" * 39 + "…"),
    ],
)
def test_should_generate_deterministic_title(content: str, expected: str) -> None:
    assert generate_conversation_title(content) == expected


def test_should_keep_default_title_until_storable_user_content_exists(
    tmp_path: Path,
) -> None:
    repository = create_repository(tmp_path / "history.db")
    conversation = repository.create_conversation("miori")

    repository.create_privacy_skipped_turn(
        "miori",
        conversation.conversation_id,
        PrivacySkippedTurnInput(
            reason_code=HistoryDecisionReasonCode.STORAGE_OPT_OUT,
            sanitizer_version="history-sanitizer-v1",
            policy_version="privacy-policy-v1",
        ),
    )

    resumed = repository.resume_conversation("miori", conversation.conversation_id)
    assert resumed.title == DEFAULT_CONVERSATION_TITLE
    assert resumed.title_is_manual is False


def test_should_generate_title_from_first_storable_user_content(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "history.db")
    conversation = repository.create_conversation("miori")

    repository.create_processing_turn(
        "miori",
        conversation.conversation_id,
        ProcessingTurnInput(sanitized_user_content="  今日の予定は？ 次の文です。"),
    )
    repository.create_processing_turn(
        "miori",
        conversation.conversation_id,
        ProcessingTurnInput(sanitized_user_content="二つ目の質問"),
    )

    resumed = repository.resume_conversation("miori", conversation.conversation_id)
    assert resumed.title == "今日の予定は？"
    assert resumed.title_is_manual is False


def test_should_remove_automatic_title_when_turn_becomes_privacy_skipped(
    tmp_path: Path,
) -> None:
    repository = create_repository(tmp_path / "history.db")
    conversation = repository.create_conversation("miori")
    turn = repository.create_processing_turn(
        "miori",
        conversation.conversation_id,
        ProcessingTurnInput(sanitized_user_content="保存してはいけない質問"),
    )

    repository.skip_processing_turn_for_privacy(
        "miori",
        conversation.conversation_id,
        turn.turn_id,
        PrivacySkippedTurnInput(
            reason_code=HistoryDecisionReasonCode.SCAN_FAILURE,
            sanitizer_version="history-sanitizer-v1",
            policy_version="privacy-policy-v1",
        ),
    )

    resumed = repository.resume_conversation("miori", conversation.conversation_id)
    assert resumed.title == DEFAULT_CONVERSATION_TITLE


def test_should_never_overwrite_manual_title(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "history.db")
    conversation = repository.create_conversation("miori")

    renamed = repository.rename_conversation(
        "miori",
        conversation.conversation_id,
        "  手動タイトル  ",
    )
    repository.create_processing_turn(
        "miori",
        conversation.conversation_id,
        ProcessingTurnInput(sanitized_user_content="自動生成候補"),
    )

    resumed = repository.resume_conversation("miori", conversation.conversation_id)
    assert renamed.title == "手動タイトル"
    assert resumed.title == "手動タイトル"
    assert resumed.title_is_manual is True


@pytest.mark.parametrize(
    "title",
    ["", "   ", "改行\nタイトル", "a" * 41],
)
def test_should_reject_invalid_manual_title(title: str) -> None:
    with pytest.raises(ValueError):
        normalize_manual_conversation_title(title)


def test_should_normalize_manual_title_to_nfc() -> None:
    assert normalize_manual_conversation_title("  Cafe\u0301  ") == "Café"
