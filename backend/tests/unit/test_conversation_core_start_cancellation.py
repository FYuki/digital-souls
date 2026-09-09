"""開始通知の配送待機を跨いで、終了済み応答の生成が開始されないことを確認する。"""
import asyncio

import pytest

from app.conversation_core import ConversationCoreSession, TextDelta
from tests.conversation_core_test_support import (
    RecordingLlm, RecordingObservation, RecordingPersistence,
    RecordingStt, RecordingTts,
)


@pytest.mark.parametrize('terminal', ['cancel', 'disconnect', 'end', 'failure', 'privacy_skip'])
def test_terminal_during_start_delivery_cannot_launch_provider_work(terminal):
    async def exercise():
        class DelayedStartDelivery:
            def __init__(self):
                self.started=asyncio.Event();self.release=asyncio.Event();self.cancelled=asyncio.Event()
            async def publish(self,event):
                if event.type!='response_started':return
                self.started.set()
                try:await self.release.wait()
                except asyncio.CancelledError:
                    # 外部I/Oが既に送信済みで取消を取り消せない場合を再現する。
                    self.cancelled.set()
                    await self.release.wait()
        delivery=DelayedStartDelivery()
        llm=RecordingLlm(deltas=(TextDelta(1,'応答。',(0,3)),))
        tts=RecordingTts()
        persistence=RecordingPersistence()
        session=ConversationCoreSession(session_id='session',response_id_factory=lambda:'response',
            delivery=delivery,persistence=persistence,observation=RecordingObservation(),
            stt=RecordingStt(),llm=llm,tts=tts)
        starting=asyncio.create_task(session.finalize_utterance(utterance_id='utterance',
            transcript='開始通知待機中に終了する',should_response=True))
        await asyncio.wait_for(delivery.started.wait(),.5)
        generation=session.response('response').generation
        async def terminate():
            if terminal=='cancel':await session.cancel_response(response_id='response',reason='barge_in')
            elif terminal=='disconnect':await session.disconnect()
            elif terminal=='end':await session.end()
            elif terminal=='failure':await session.fail_response(response_id='response',generation=generation)
            else:await session.privacy_skip_response(response_id='response',generation=generation)
        ending=asyncio.create_task(terminate())
        async def wait_for_terminal():
            while not session.response('response').state.is_terminal:await asyncio.sleep(0)
        await asyncio.wait_for(wait_for_terminal(),.5)
        if terminal in ('cancel','disconnect','end'):
            await asyncio.wait_for(delivery.cancelled.wait(),.5)
        delivery.release.set()
        await asyncio.wait_for(asyncio.gather(starting,ending),.5)
        for _ in range(20):await asyncio.sleep(0)
        await session.end()
        assert llm.calls==[]
        assert tts.calls==[]
        assert len(persistence.outcomes)==1
        assert session.running_stage_count==0
    asyncio.run(exercise())
