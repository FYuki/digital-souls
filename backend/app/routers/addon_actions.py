"""内部管理・チャットからの承認回答。回答自体は外部操作を実行しない。"""

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, UUID4

from app.addon_action.models import ApprovalChoice
from app.addon_action.policy import ActionPolicy
from app.external_mcp.models import MCPFailure
from app.routers.validation import SafeValidationRoute
from app.addon_action.interaction import confirmation_resume_scope
from app.addon_action.models import ExecutionScene
from app.routers.chat import ChatRequest, PersistedChatResponse, chat
from app.conversation_core.session import TerminalProtocolError
from app.tool_use.service import ToolService

router = APIRouter(prefix="/addon-actions", route_class=SafeValidationRoute)


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: ApprovalChoice
    character: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)


class ContinueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character: str = Field(min_length=1, max_length=128)
    conversation_id: UUID4
    voice_session_id: UUID4 | None = None


def _policy(request: Request) -> ActionPolicy:
    policy: ActionPolicy | None = getattr(request.app.state, "action_policy", None)
    if policy is None:
        raise HTTPException(503, "承認機能を利用できません。")
    return policy


@router.get("/requests")
async def requests(
    request: Request,
    response: Response,
    character: str | None = None,
    session_id: str | None = None,
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    policy = _policy(request)
    return {
        "requests": [
            r.public()
            for r in policy.store.requests(
                character_id=character, session_id=session_id
            )
        ]
    }


@router.post("/requests/{request_id}/answer")
async def answer(
    request_id: UUID4, payload: AnswerInput, request: Request, response: Response
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    policy = _policy(request)
    try:
        pending = policy.store.request(str(request_id))
        if (
            pending.character_id != payload.character
            or pending.session_id != payload.session_id
        ):
            raise MCPFailure("policy", "confirmation_mismatch")
        connection = request.app.state.addon_manager.gate.registry.entry(
            pending.key.connection_id
        ).connection
        if connection.identity != pending.key.connection_identity:
            raise MCPFailure("policy", "connection_changed")
        result = policy.store.answer(str(request_id), payload.choice)
    except MCPFailure:
        raise HTTPException(
            409, "この確認要求には回答できません。状態を再取得してください。"
        ) from None
    # 外部dispatchやruntime再開はここでは行わない。
    return {"request": result.public()}


@router.post("/requests/{request_id}/continue", response_model=None)
async def continue_conversation(
    request_id: UUID4,
    payload: ContinueInput,
    request: Request,
    response: Response,
) -> PersistedChatResponse | JSONResponse | dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    policy = _policy(request)
    service: ToolService | None = getattr(request.app.state, "tool_service", None)
    rid, conversation = str(request_id), str(payload.conversation_id)
    try:
        pending = policy.store.request(rid)
        if (
            pending.character_id != payload.character
            or pending.session_id != conversation
            or pending.key.scene != ExecutionScene.CONVERSATION
            or pending.choice is None
        ):
            raise MCPFailure("policy", "confirmation_mismatch")
    except MCPFailure:
        raise HTTPException(409, "この確認要求から会話を続行できません。") from None

    def waiting() -> bool:
        return bool(
            service
            and service.pending_confirmation(payload.character, conversation, rid)
        )

    if not pending.waiting or policy.clock() >= pending.wait_until or not waiting():
        return {"state": "ended"}
    assert service is not None
    if not service.claim_confirmation(payload.character, conversation, rid):
        return {"state": "continuing"}
    keep_claim = False
    try:
        labels = {
            ApprovalChoice.ALWAYS: "画面で「常に承認する」を選択しました。",
            ApprovalChoice.ONCE: "画面で「一度承認する」を選択しました。",
            ApprovalChoice.REJECT: "画面で「拒否する」を選択しました。",
        }
        message = labels[pending.choice]
        if payload.voice_session_id is not None:
            manager = getattr(request.app.state, "livekit_runtime_manager", None)
            if manager is None:
                raise HTTPException(503, "音声会話を利用できません。")
            try:
                started = await manager.submit_action_confirmation(
                    str(payload.voice_session_id),
                    payload.character,
                    conversation,
                    rid,
                    message,
                    waiting,
                )
            except (ValueError, TimeoutError, TerminalProtocolError):
                raise HTTPException(
                    409,
                    "承認は保存しました。音声の応答が終わってから続行してください。",
                ) from None
            keep_claim = started
            return {"state": "voice_started" if started else "ended"}
        with confirmation_resume_scope(payload.character, conversation, rid):
            result = await chat(
                ChatRequest(
                    character=payload.character,
                    conversation_id=payload.conversation_id,
                    message=message,
                ),
                request,
            )
            if isinstance(result, JSONResponse):
                result.headers["Cache-Control"] = "no-store"
            return result
    finally:
        if not keep_claim:
            service.release_confirmation(payload.character, conversation, rid)
