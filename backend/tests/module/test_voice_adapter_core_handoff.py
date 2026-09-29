"""LiveKitなしのテスト用Voice adapter経由で、Coreへの受渡しと中断履歴を検証する。

対象: Issue #540。同一 ConversationCoreSession で
「入力→応答生成・音声区間の送出→割込→保存」を連続して観測する。
再生済み範囲は BE の送出位置から推定し、FE 報告は推定を上書きしない。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.conversation_core import (
    AudioSegment,
    ConversationCoreSession,
    Response,
    ResponseState,
    TextDelta,
)
from app.conversation_core.adapters import ConversationHistoryPersistenceAdapter
from app.conversation_history.models import TurnStatus
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.service import ConversationHistorySession
from app.privacy.contracts import (
    ConversationHistoryAction,
    ConversationHistoryDecision,
    HistoryDecisionReasonCode,
)
from tests.conversation_core_test_support import (
    RecordingObservation,
    RecordingStt,
    response_id_factory,
)
from tests.conversation_history_test_support import create_repository
from tests.voice_adapter_test_support import (
    VoiceAdapterCoreHandoff,
    VoiceAdapterDelivery,
)


FIRST_SENTENCE = "一文目です。"
SECOND_SENTENCE = "二文目です。"
GENERATED_TEXT = FIRST_SENTENCE + SECOND_SENTENCE


class TwoSentenceLlm:
    """区切り済みの二文を1deltaで返す。

    `hold_response_ids` に含まれる応答はdelta送出後に `release` がsetされる
    まで待機し、割込・取消の時点で旧応答が進行中であることを保証する。
    """

    def __init__(self, *, hold_response_ids: set[str]) -> None:
        self._hold_response_ids = hold_response_ids
        self.release = asyncio.Event()

    async def generate(
        self,
        transcript: str,
        *,
        response: Response,
    ):
        yield TextDelta(1, GENERATED_TEXT, (0, len(GENERATED_TEXT)))
        if response.response_id in self._hold_response_ids:
            await self.release.wait()


class SentenceTts:
    """セグメント単位のテキストへ1区間の音声を割り当てる。"""

    async def synthesize(self, text: str):
        yield AudioSegment(1, b"pcm-" + text.encode(), (0, len(text)))


def _make_sanitizer() -> MagicMock:
    sanitizer = MagicMock()

    def sanitize(text: str) -> ConversationHistoryDecision:
        return ConversationHistoryDecision(
            action=ConversationHistoryAction.STORE_MASKED,
            reason_code=HistoryDecisionReasonCode.MASKED,
            sanitizer_version="test",
            policy_version="test",
            content=text,
        )

    sanitizer.sanitize_current_user.side_effect = sanitize
    sanitizer.sanitize_assistant.side_effect = sanitize
    return sanitizer


def _build_session(
    tmp_path: Path,
    *,
    estimated_prefix: int,
    response_ids: tuple[str, ...] = (),
):
    """BE送出位置から推定された再生済み範囲を返す session を構築する。

    `estimated_prefix` は BE が送出位置から推定した `audio_sequence` 数。
    FE の `last_played_audio_sequence` 報告ではなく、BE の推定値が使われる。
    """
    repository = create_repository(tmp_path / "voice_adapter.db", uuid_factory=uuid4)
    conversation = repository.create_conversation("miori")
    history = ConversationHistorySession(
        "miori",
        conversation.conversation_id,
        repository,
        _make_sanitizer(),
    )
    persistence = ConversationHistoryPersistenceAdapter(history_session=history)
    delivery = VoiceAdapterDelivery()
    adapter = VoiceAdapterCoreHandoff(
        played_prefix_source=lambda _response: estimated_prefix,
    )
    ids = response_ids or (str(uuid4()), str(uuid4()))
    llm = TwoSentenceLlm(hold_response_ids={ids[0]})
    session = ConversationCoreSession(
        session_id=str(uuid4()),
        response_id_factory=response_id_factory(*ids),
        delivery=delivery,
        persistence=persistence,
        observation=RecordingObservation(),
        stt=RecordingStt(),
        llm=llm,
        tts=SentenceTts(),
        cancellation=adapter,
    )
    adapter.attach_session(session)
    return session, adapter, delivery, repository, conversation, ids, llm


async def _wait_delivered_audio_segments(
    delivery: VoiceAdapterDelivery,
    count: int,
    *,
    response_id: str,
    timeout: float = 5.0,
) -> None:
    async def poll() -> None:
        while len([
            event
            for event in delivery.events_of("response_audio_segment")
            if event.response_id == response_id
        ]) < count:
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


async def _wait_delivery_event(
    delivery: VoiceAdapterDelivery,
    event_type: str,
    *,
    response_id: str,
    timeout: float = 5.0,
) -> None:
    """指定した応答IDの配送イベントが記録されるまで待つ。"""
    async def poll() -> None:
        while not any(
            event.response_id == response_id
            for event in delivery.events_of(event_type)
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


async def _wait_turn_status(
    repository: ConversationHistoryRepository,
    character_id: str,
    conversation_id: UUID,
    status: TurnStatus,
    *,
    timeout: float = 5.0,
) -> None:
    async def poll() -> None:
        while not any(
            turn.status is status
            for turn in repository.list_turns(character_id, conversation_id)
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


def test_interrupt_persists_estimated_prefix_and_rejects_late_output(tmp_path: Path) -> None:
    """中断時に BE 推定値で履歴が保存され、旧応答の遅延出力は拒否される。"""
    async def run():
        # BE推定: 1セグメントまで再生済みと推定
        session, adapter, delivery, repository, conversation, ids, llm = _build_session(
            tmp_path, estimated_prefix=1,
        )
        response_id = ids[0]

        # 第一発話 → 応答生成と音声区間送出
        response = await adapter.submit_utterance(
            utterance_id=str(uuid4()),
            transcript="最初の質問",
        )
        assert response is not None
        assert response.response_id == response_id
        await _wait_delivered_audio_segments(delivery, 2, response_id=response_id)

        # 2区間の配送後も旧応答は進行中である
        audio_events = [
            event
            for event in delivery.events_of("response_audio_segment")
            if event.response_id == response_id
        ]
        assert len(audio_events) == 2
        assert [event.audio_sequence for event in audio_events] == [1, 2]
        assert session.response(response_id).state is ResponseState.IN_PROGRESS

        # 新発話を保留し旧応答を割込停止。BE推定値=1 が使われる。
        cancelled = await adapter.interrupt_with_utterance(
            utterance_id=str(uuid4()),
            transcript="次の質問",
            interrupted_response_id=response_id,
        )
        assert cancelled is not None
        terminal = await adapter.wait_for_terminal(response_id=response_id)
        assert terminal.state is ResponseState.CANCELLED
        assert len(adapter.stopped_responses) == 1
        assert adapter.stopped_responses[0].response_id == response_id

        # 中断履歴: SQLite の turn が第一文だけを保持し interrupted になる
        await _wait_turn_status(
            repository, "miori", conversation.conversation_id, TurnStatus.INTERRUPTED,
        )
        turns = repository.list_turns("miori", conversation.conversation_id)
        interrupted = next(turn for turn in turns if turn.status is TurnStatus.INTERRUPTED)
        assert interrupted.user_content == "最初の質問"
        assert interrupted.assistant_content == FIRST_SENTENCE

        # 旧応答の遅延出力は採用されない
        assert not await session.accept_text_delta(
            response_id=response_id,
            generation=response.generation,
            text_sequence=2,
            text="遅延",
            text_range=(len(GENERATED_TEXT), len(GENERATED_TEXT) + 2),
        )
        assert not await session.accept_audio_segment(
            response_id=response_id,
            generation=response.generation,
            audio_sequence=3,
            audio=b"late",
            text_range=(0, 1),
        )
        assert len([
            event
            for event in delivery.events_of("response_audio_segment")
            if event.response_id == response_id
        ]) == 2

        # 次応答は同じSessionで開始され、中断履歴と別turnになる。
        await _wait_delivery_event(
            delivery, "response_started", response_id=ids[1],
        )
        llm.release.set()
        await adapter.wait_for_terminal(response_id=ids[1])
        await _wait_turn_status(
            repository, "miori", conversation.conversation_id, TurnStatus.COMPLETED,
        )
        turns = repository.list_turns("miori", conversation.conversation_id)
        assert len(turns) == 2
        completed = next(turn for turn in turns if turn.status is TurnStatus.COMPLETED)
        assert completed.user_content == "次の質問"
        assert completed.assistant_content == GENERATED_TEXT

        await session.end()

    asyncio.run(run())


def test_interrupt_persists_full_text_when_estimated_prefix_covers_all(tmp_path: Path) -> None:
    """BE推定値が全区間をカバーする場合、全文が履歴に保存される。"""
    async def run():
        session, adapter, delivery, repository, conversation, ids, llm = _build_session(
            tmp_path, estimated_prefix=2,
        )
        response_id = ids[0]

        response = await adapter.submit_utterance(
            utterance_id=str(uuid4()),
            transcript="質問",
        )
        assert response is not None
        await _wait_delivered_audio_segments(delivery, 2, response_id=response_id)
        assert session.response(response_id).state is ResponseState.IN_PROGRESS

        cancelled = await adapter.interrupt_with_utterance(
            utterance_id=str(uuid4()),
            transcript="追加入力",
            interrupted_response_id=response_id,
        )
        assert cancelled is not None
        terminal = await adapter.wait_for_terminal(response_id=response_id)
        assert terminal.state is ResponseState.CANCELLED
        llm.release.set()
        await _wait_turn_status(
            repository, "miori", conversation.conversation_id, TurnStatus.INTERRUPTED,
        )

        turns = repository.list_turns("miori", conversation.conversation_id)
        interrupted = next(turn for turn in turns if turn.status is TurnStatus.INTERRUPTED)
        assert interrupted.assistant_content == GENERATED_TEXT
        await session.end()

    asyncio.run(run())


def test_explicit_cancel_response_also_uses_estimated_prefix(tmp_path: Path) -> None:
    """明示的な取消でも BE 推定値が使われる。"""
    async def run():
        session, adapter, delivery, repository, conversation, ids, llm = _build_session(
            tmp_path, estimated_prefix=1,
        )
        response_id = ids[0]

        response = await adapter.submit_utterance(
            utterance_id=str(uuid4()),
            transcript="質問",
        )
        assert response is not None
        await _wait_delivered_audio_segments(delivery, 2, response_id=response_id)
        assert session.response(response_id).state is ResponseState.IN_PROGRESS

        cancelled = await adapter.cancel_response(
            response_id=response_id,
            reason="user_cancelled",
        )
        assert cancelled is not None
        terminal = await adapter.wait_for_terminal(response_id=response_id)
        assert terminal.state is ResponseState.CANCELLED
        llm.release.set()
        await _wait_turn_status(
            repository, "miori", conversation.conversation_id, TurnStatus.INTERRUPTED,
        )

        turns = repository.list_turns("miori", conversation.conversation_id)
        interrupted = next(turn for turn in turns if turn.status is TurnStatus.INTERRUPTED)
        assert interrupted.assistant_content == FIRST_SENTENCE
        await session.end()

    asyncio.run(run())


def test_interrupt_with_zero_estimated_prefix_saves_empty_content(tmp_path: Path) -> None:
    """送出前の中断では空の再生済み範囲が保存される。"""
    async def run():
        session, adapter, delivery, repository, conversation, ids, llm = _build_session(
            tmp_path, estimated_prefix=0,
        )
        response_id = ids[0]

        response = await adapter.submit_utterance(
            utterance_id=str(uuid4()),
            transcript="質問",
        )
        assert response is not None
        await _wait_delivered_audio_segments(delivery, 2, response_id=response_id)
        assert session.response(response_id).state is ResponseState.IN_PROGRESS

        # 送出前に中断（BE推定値=0）
        cancelled = await adapter.cancel_response(
            response_id=response_id,
            reason="user_cancelled",
        )
        assert cancelled is not None
        terminal = await adapter.wait_for_terminal(response_id=response_id)
        assert terminal.state is ResponseState.CANCELLED
        llm.release.set()
        await _wait_turn_status(
            repository, "miori", conversation.conversation_id, TurnStatus.INTERRUPTED,
        )

        turns = repository.list_turns("miori", conversation.conversation_id)
        interrupted = next(turn for turn in turns if turn.status is TurnStatus.INTERRUPTED)
        # 再生済み範囲が0なので、assistant_contentは空
        assert interrupted.assistant_content == ""
        await session.end()

    asyncio.run(run())


def test_fe_report_does_not_override_be_estimation(tmp_path: Path) -> None:
    """FE の再生報告は BE 推定値を上書きしない。

    新実装では再生済みprefixは `stop_response` が返す BE 推定値だけが使われ、
    FE 経路から Core の `last_played_audio_sequence` を更新する入口はない。
    adapter 側に記録された FE 報告値があっても、履歴は推定値で保存される。
    """
    async def run():
        # BE推定: 1セグメント。FE報告: 2セグメント（異なる値）
        session, adapter, delivery, repository, conversation, ids, llm = _build_session(
            tmp_path, estimated_prefix=1,
        )
        response_id = ids[0]

        response = await adapter.submit_utterance(
            utterance_id=str(uuid4()),
            transcript="質問",
        )
        assert response is not None
        await _wait_delivered_audio_segments(delivery, 2, response_id=response_id)

        # FE が 2 区間再生を報告しても、Core 側の参照先は BE 推定値のみ。
        adapter.report_fe_playback(response_id, 2)

        cancelled = await adapter.cancel_response(
            response_id=response_id,
            reason="barge_in",
        )
        assert cancelled is not None
        terminal = await adapter.wait_for_terminal(response_id=response_id)
        assert terminal.state is ResponseState.CANCELLED
        llm.release.set()
        await _wait_turn_status(
            repository, "miori", conversation.conversation_id, TurnStatus.INTERRUPTED,
        )

        turns = repository.list_turns("miori", conversation.conversation_id)
        interrupted = next(turn for turn in turns if turn.status is TurnStatus.INTERRUPTED)
        assert interrupted.assistant_content == FIRST_SENTENCE
        await session.end()

    asyncio.run(run())


def test_core_and_invocation_do_not_import_livekit() -> None:
    """Core / core_invocation がLiveKit固有型を直接importしないことを確認する。"""
    backend_app = Path(__file__).resolve().parents[2] / "app"
    targets = [
        backend_app / "core_invocation.py",
        *sorted((backend_app / "conversation_core").glob("*.py")),
    ]
    violations: list[str] = []
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module_names: list[str] = []
            if isinstance(node, ast.Import):
                module_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    module_names.append(node.module)
                module_names.extend(alias.name for alias in node.names)
            for name in module_names:
                if "livekit" in name.lower():
                    violations.append(f"{path.name}:{node.lineno}:{name}")
    assert violations == []
