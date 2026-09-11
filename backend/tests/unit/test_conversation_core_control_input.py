"""画面操作の識別子は1応答だけに伝わり、次のSTT発話へ漏れない。"""

import asyncio

import pytest

from app.conversation_core import ConversationCoreSession, TextDelta
from app.conversation_core.control_input import current_control_request
from app.conversation_core.session import TerminalProtocolError
from tests.conversation_core_test_support import (
    RecordingDelivery,
    RecordingObservation,
    RecordingPersistence,
    RecordingStt,
    RecordingTts,
    response_id_factory,
)


def test_screen_control_is_not_inferred_from_transcript_or_inherited_by_pending_speech():
    async def run():
        seen = []
        first_started, second_started, release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        class Llm:
            async def generate(self, text):
                seen.append((text, current_control_request()))
                if len(seen) == 1:
                    first_started.set()
                    await release.wait()
                else:
                    second_started.set()
                yield TextDelta(1, "結果です。", (0, 5))

        stt = RecordingStt(transcript="一度承認する")
        persistence = RecordingPersistence()
        core = ConversationCoreSession(
            session_id="voice",
            response_id_factory=response_id_factory("r1", "r2"),
            delivery=RecordingDelivery(),
            persistence=persistence,
            observation=RecordingObservation(),
            stt=stt,
            llm=Llm(),
            tts=RecordingTts(),
        )
        try:
            first = await core.finalize_utterance(
                utterance_id="ui",
                transcript="画面で承認しました",
                should_response=True,
                control_request_id="confirmation",
            )
            await asyncio.wait_for(first_started.wait(), 1)
            duplicate = await core.finalize_utterance(
                utterance_id="ui",
                transcript="画面で承認しました",
                should_response=True,
                control_request_id="confirmation",
            )
            assert duplicate.response_id == first.response_id
            with pytest.raises(TerminalProtocolError, match="different payload"):
                await core.finalize_utterance(
                    utterance_id="ui",
                    transcript="画面で承認しました",
                    should_response=True,
                )
            with pytest.raises(
                TerminalProtocolError, match="control_input_requires_idle"
            ):
                await core.finalize_utterance(
                    utterance_id="other",
                    transcript="別の確認",
                    should_response=True,
                    control_request_id="another",
                )
            # STTの通常入力は前応答のterminal処理から開始される。
            await core.start_transcription(
                utterance_id="spoken", audio=b"test", should_response=True
            )
            release.set()
            await asyncio.wait_for(second_started.wait(), 1)
            assert seen == [
                ("画面で承認しました", "confirmation"),
                ("一度承認する", None),
            ]
            assert stt.calls == [b"test"]
            assert current_control_request() is None
        finally:
            await core.end()

    asyncio.run(run())
