"""LiveKitなしで ConversationCoreSession へ voice 入力を渡すテスト用adapter。

確定発話の受理、新入力による旧応答の割込、取消、再生済みprefixの供給を
Coreの既存portへ接続する。LiveKit Room / Track / PCM / SDK型は使わない。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.conversation_core.models import (
    CoreEvent,
    Response,
    ResponseStopResult,
)

if TYPE_CHECKING:
    from app.conversation_core.session import ConversationCoreSession


@dataclass
class VoiceAdapterDelivery:
    """Coreから配送されたイベントを順序付きで記録する。"""

    events: list[CoreEvent] = field(default_factory=list)

    async def publish(self, event: CoreEvent) -> None:
        self.events.append(event)

    def events_of(self, event_type: str) -> list[CoreEvent]:
        return [event for event in self.events if event.type == event_type]


@dataclass
class VoiceAdapterCoreHandoff:
    """テスト用Voice adapter。

    `cancellation` port として `ConversationCoreSession` へ渡し、
    `attach_session` で session を設定してから発話操作を行う。
    `played_prefix_source` は応答ごとの再生済み範囲を供給する差し替え点で、
    `Callable[[Response], int]` の返り値が
    `ResponseStopResult.last_played_audio_sequence` としてCoreへ届く。
    """

    played_prefix_source: Callable[[Response], int]
    stopped_responses: list[Response] = field(default_factory=list)
    _session: ConversationCoreSession | None = field(default=None, init=False, repr=False)

    def attach_session(self, session: ConversationCoreSession) -> None:
        """発話操作の対象となる Session を設定する。

        Session 構築時に `cancellation=adapter` として渡す必要があるため、
        adapter を先に生成し、Session 構築後にこのメソッドで接続する。
        """
        self._session = session

    async def submit_utterance(
        self,
        *,
        utterance_id: str,
        transcript: str,
        should_response: bool = True,
    ) -> Response | None:
        """確定した発話をCoreへ渡す。"""
        session = self._require_session()
        return await session.finalize_utterance(
            utterance_id=utterance_id,
            transcript=transcript,
            should_response=should_response,
        )

    async def interrupt_with_utterance(
        self,
        *,
        utterance_id: str,
        transcript: str,
        interrupted_response_id: str,
    ) -> Response | None:
        """新しい発話を受理し、進行中の応答を割り込み停止させる。

        既存の状態遷移に沿い、新入力を保留してから旧応答を取消す。
        """
        session = self._require_session()
        await session.finalize_utterance(
            utterance_id=utterance_id,
            transcript=transcript,
            should_response=True,
        )
        return await session.cancel_response(
            response_id=interrupted_response_id,
            reason="barge_in",
        )

    async def cancel_response(self, *, response_id: str, reason: str) -> Response | None:
        """応答を明示的に取消す。"""
        session = self._require_session()
        return await session.cancel_response(
            response_id=response_id,
            reason=reason,
        )

    async def stop_response(self, response: Response) -> ResponseStopResult:
        """Coreからの出力停止要求に再生済みprefixを返す。

        ResponseCancellationPort の実装。注入された供給関数の値を
        `last_played_audio_sequence` としてCoreへ届ける。
        """
        self.stopped_responses.append(response)
        last_played = self.played_prefix_source(response)
        return ResponseStopResult(last_played_audio_sequence=last_played)

    async def wait_for_terminal(
        self,
        *,
        response_id: str,
        timeout: float = 5.0,
    ) -> Response:
        """応答が終端状態へ到達するまで待つ。"""
        session = self._require_session()

        async def poll() -> None:
            while not session.response(response_id).state.is_terminal:
                await asyncio.sleep(0)

        await asyncio.wait_for(poll(), timeout=timeout)
        return session.response(response_id)

    def _require_session(self) -> ConversationCoreSession:
        if self._session is None:
            raise RuntimeError("attach_session must be called before voice operations")
        return self._session
