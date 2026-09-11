"""承認保存・明示続行・会話runtimeをHTTP境界で接続する。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.addon_action.models import ApprovalChoice
from app.tool_use.routing import ToolDecision
from app.routers import addon_actions
from tests.addon_action_test_support import policy
from tests.tool_use_test_support import Decisions, call, runtime


@pytest.mark.parametrize("direct_response", [False, True])
def test_http_continue_is_bound_to_saved_answer_and_duplicate_does_not_restart(
    monkeypatch, tmp_path, direct_response
):
    async def run():
        conversation = str(uuid4())
        async with runtime(Decisions(call, ToolDecision("finish"))) as (
            service,
            source,
            gate,
        ):
            gate.confirmations = p = policy(tmp_path)
            await service.run("miori", conversation, "実行して")
            rid = service.status("miori", conversation)["confirmation_id"]
            app = FastAPI()
            app.include_router(addon_actions.router)
            app.state.tool_service = service
            app.state.action_policy = p
            app.state.addon_manager = SimpleNamespace(gate=gate)
            entered, release = asyncio.Event(), asyncio.Event()

            async def chat(payload, request):
                entered.set()
                await release.wait()
                material = await service.run(
                    payload.character, str(payload.conversation_id), payload.message
                )
                body = {"results": list(material.results)}
                return JSONResponse(body) if direct_response else body

            monkeypatch.setattr(addon_actions, "chat", chat)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                url = f"/addon-actions/requests/{rid}"
                body = {"character": "miori", "conversation_id": conversation}
                assert (
                    await client.post(url + "/continue", json=body)
                ).status_code == 409
                saved = await client.post(
                    url + "/answer",
                    json={
                        "character": "miori",
                        "session_id": conversation,
                        "choice": "once",
                    },
                )
                assert saved.status_code == 200 and not source.calls
                assert (
                    await client.post(
                        url + "/continue", json={**body, "character": "other"}
                    )
                ).status_code == 409
                first = asyncio.create_task(client.post(url + "/continue", json=body))
                await asyncio.wait_for(entered.wait(), 1)
                duplicate = await client.post(url + "/continue", json=body)
                assert duplicate.json() == {"state": "continuing"}
                assert duplicate.headers["cache-control"] == "no-store"
                release.set()
                result = await first
                assert result.status_code == 200
                assert result.headers["cache-control"] == "no-store"
                assert result.json()["results"][0]["outcome"] == "succeeded"
                assert len(source.calls) == 1
                assert (await client.post(url + "/continue", json=body)).json() == {
                    "state": "ended"
                }

    asyncio.run(run())


def test_stopped_conversation_and_voice_mismatch_do_not_dispatch(monkeypatch, tmp_path):
    async def run():
        conversation, voice = str(uuid4()), str(uuid4())
        async with runtime(Decisions(call)) as (service, source, gate):
            gate.confirmations = p = policy(tmp_path)
            await service.run("miori", conversation, "実行して")
            rid = service.status("miori", conversation)["confirmation_id"]
            p.store.answer(rid, ApprovalChoice.ONCE)
            app = FastAPI()
            app.include_router(addon_actions.router)
            app.state.tool_service, app.state.action_policy = service, p
            calls = []

            async def mismatched(*args):
                calls.append(args)
                raise ValueError("wrong session")

            app.state.livekit_runtime_manager = SimpleNamespace(
                submit_action_confirmation=mismatched
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                url = f"/addon-actions/requests/{rid}/continue"
                body = {
                    "character": "miori",
                    "conversation_id": conversation,
                    "voice_session_id": voice,
                }
                assert (await client.post(url, json=body)).status_code == 409
                # 失敗後は正しい会話で再度続行可能。単回承認は未消費。
                assert p.store.request(rid).once_reserved
                assert service.claim_confirmation("miori", conversation, rid)
                service.release_confirmation("miori", conversation, rid)
                service.stop("miori", conversation)
                assert (await client.post(url, json=body)).json() == {"state": "ended"}
                assert len(calls) == 1 and not source.calls
                saved = p.store.request(rid)
                assert not saved.once_reserved
                assert p.store.state(saved.key).remaining == 0

    asyncio.run(run())
