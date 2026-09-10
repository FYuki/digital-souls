"""画面承認だけで元要求を続行し、STTやLLMから承認を推測しない。"""

import asyncio

import pytest

from app.addon_action.interaction import confirmation_resume_scope
from app.addon_action.models import ApprovalChoice
from app.external_mcp.models import MCPFailure
from app.tool_use.routing import ToolDecision
from tests.unit.test_addon_action_queue import policy
from tests.unit.test_tool_use import Decisions, call, runtime


def test_only_explicit_screen_continuation_dispatches_once(tmp_path):
    async def run():
        decisions = Decisions(call, ToolDecision("finish"))
        async with runtime(decisions) as (service, source, gate):
            gate.confirmations = p = policy(tmp_path)
            material = await service.run("miori", "session", "実行して")
            assert material.waiting and "画面" in material.direct_text
            rid = service.status("miori", "session")["confirmation_id"]
            assert not source.calls
            until = service._runs[("miori", "session")].waiting_until
            for text in ("常に承認する", "はい", "一度承認する"):
                assert (await service.run("miori", "session", text)).waiting
            assert service._runs[("miori", "session")].waiting_until == until
            assert len(decisions.contexts) == 1 and not source.calls
            p.store.answer(rid, ApprovalChoice.ONCE)
            # 保存済み回答も、新しい通常発話へ元要求の実行権を渡さない。
            assert (await service.run("miori", "session", "続けて")).waiting
            with confirmation_resume_scope("other", "session", rid):
                assert (await service.run("miori", "session", "続けて")).waiting
            with confirmation_resume_scope("miori", "session", rid):
                result = await service.run("miori", "session", "画面で一度承認しました")
            assert result.results[0]["outcome"] == "succeeded"
            assert len(source.calls) == 1
            assert not service.pending_confirmation("miori", "session", rid)
            with confirmation_resume_scope("miori", "session", rid):
                duplicate = await service.run("miori", "session", "重複した続行")
            assert "終了" in duplicate.direct_text and len(source.calls) == 1

    asyncio.run(run())


def test_refusal_does_not_dispatch_and_next_request_can_ask_again(tmp_path):
    async def run():
        async with runtime(Decisions(call, call)) as (service, source, gate):
            gate.confirmations = p = policy(tmp_path)
            await service.run("miori", "session", "実行して")
            rid = service.status("miori", "session")["confirmation_id"]
            p.store.answer(rid, ApprovalChoice.REJECT)
            with confirmation_resume_scope("miori", "session", rid):
                result = await service.run("miori", "session", "拒否しました")
            assert result.results[0]["outcome"] == "rejected"
            assert not source.calls
            assert (await service.run("miori", "session", "もう一度依頼する")).waiting
            assert service.status("miori", "session")["confirmation_id"] != rid

    asyncio.run(run())


def test_saved_screen_context_is_revalidated_on_continuation(tmp_path):
    async def run():
        valid = [True]

        async def before():
            if not valid[0]:
                raise MCPFailure("policy", "screen_context_revoked")

        async with runtime(Decisions(call)) as (service, source, gate):
            gate.confirmations = p = policy(tmp_path)
            await service.run(
                "miori", "session", "画面の内容で実行", before_execute=before
            )
            rid = service.status("miori", "session")["confirmation_id"]
            p.store.answer(rid, ApprovalChoice.ONCE)
            valid[0] = False
            with confirmation_resume_scope("miori", "session", rid):
                await service.run("miori", "session", "承認しました")
            assert not source.calls
            assert p.store.state(p.store.request(rid).key).remaining == 1

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["stop", "disable"])
def test_stopped_confirmation_only_grants_future_permission(tmp_path, reason):
    async def run():
        async with runtime(Decisions(call)) as (service, source, gate):
            gate.confirmations = p = policy(tmp_path)
            await service.run("miori", "session", "実行して")
            rid = service.status("miori", "session")["confirmation_id"]
            if reason == "stop":
                service.stop("miori", "session")
            else:
                service.connection_disabled(source.connection.id)
            p.store.answer(rid, ApprovalChoice.ONCE)
            with confirmation_resume_scope("miori", "session", rid):
                result = await service.run("miori", "session", "承認しました")
            assert "終了" in result.direct_text
            assert (
                not source.calls
                and p.store.state(p.store.request(rid).key).remaining == 1
            )

    asyncio.run(run())
