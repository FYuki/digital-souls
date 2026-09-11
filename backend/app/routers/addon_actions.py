"""内部管理・チャットからの承認回答。回答自体は外部操作を実行しない。"""

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, UUID4
from uuid import UUID

from app.addon_action.models import ApprovalChoice, ApprovalKey, OperationGroup, Permission
from app.addon_action.store import ConfirmationRequest
from app.addon_action.policy import ActionPolicy
from app.external_mcp.models import MCPFailure
from app.routers.validation import SafeValidationRoute
from app.addon_action.interaction import confirmation_resume_scope
from app.addon_action.models import ExecutionScene
from app.routers.chat import ChatRequest, PersistedChatResponse, chat
from app.conversation_core.session import TerminalProtocolError
from app.tool_use.service import ToolService
from app.external_mcp.models import Json, digest

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
        current = policy.store.request(rid)
        return bool(
            service
            and current.waiting
            and policy.clock() < current.wait_until
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


class AdminAnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: ApprovalChoice


class PermissionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str = Field(min_length=1, max_length=256)
    connection_token: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    operation_group: OperationGroup
    scene: ExecutionScene
    permission: Permission


def _connection_token(connection_id: str, identity: str) -> str:
    return digest([connection_id, identity])


def _admin_projection(request: Request, item: ConfirmationRequest) -> Json:
    policy = _policy(request)
    available = False
    try:
        entry = request.app.state.addon_manager.gate.registry.entry(item.key.connection_id)
        available = entry.linked and entry.connection.identity == item.key.connection_identity
    except MCPFailure:
        pass
    # 取得時にも現在のsecret redactionを適用し、生の引数・fingerprint等は返さない。
    preview = policy.sanitizer.value(item.preview)
    return {
        **item.public(), "preview": preview,
        "character_id": item.character_id, "session_id": item.session_id,
        "waiting": item.waiting and policy.clock() < item.wait_until,
        "connection_available": available,
        "once_reserved": item.once_reserved and item.waiting and policy.clock() < item.wait_until,
    }


@router.get("/admin/requests")
async def admin_requests(
    request: Request, response: Response, unanswered: bool = True,
    before: UUID4 | None = None, limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    try:
        page, cursor = _policy(request).store.request_page(
            unanswered=unanswered, before=str(before) if before else None, limit=limit,
        )
    except MCPFailure:
        raise HTTPException(409, "確認キューを先頭から再取得してください。") from None
    return {"requests": [_admin_projection(request, item) for item in page], "next_cursor": cursor}


@router.get("/admin/permissions")
async def admin_permissions(request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    policy = _policy(request)
    rows: list[tuple[ApprovalKey, Json]] = []
    for entry in request.app.state.addon_manager.gate.registry.entries():
        if not entry.linked:
            continue
        connection = entry.connection
        for group in OperationGroup:
            for scene in ExecutionScene:
                key = ApprovalKey(connection.id, connection.identity, group, scene)
                rows.append((key, {
                    "connection_id": connection.id,
                    "connection_token": _connection_token(connection.id, connection.identity),
                    "connection_label": policy.sanitizer.text(entry.display_name, maximum=128),
                    "operation_group": group, "scene": scene,
                }))
    settings = policy.store.settings_many(key for key, _ in rows)
    return {"permissions": [{**row, **settings[key]} for key, row in rows]}


@router.put("/admin/permissions")
async def update_permission(
    payload: PermissionInput, request: Request, response: Response,
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    policy = _policy(request)
    try:
        entry = request.app.state.addon_manager.gate.registry.entry(payload.connection_id)
        connection = entry.connection
        if not entry.linked or payload.connection_token != _connection_token(connection.id, connection.identity):
            raise MCPFailure("policy", "connection_changed")
        key = ApprovalKey(connection.id, connection.identity, payload.operation_group, payload.scene)
        # awaitを挟まずidentity照合と設定変更を行う。ON/OFF・grantは変更しない。
        ended = policy.store.set_permission(key, payload.permission)
        service: ToolService | None = getattr(request.app.state, "tool_service", None)
        if service is not None:
            service.end_idle_confirmations(ended)
        return {"setting": policy.store.settings(key)}
    except MCPFailure:
        raise HTTPException(409, "承認設定を変更できません。状態を再取得してください。") from None


@router.post("/admin/requests/{request_id}/answer")
async def admin_answer(
    request_id: UUID4, payload: AdminAnswerInput, request: Request, response: Response,
) -> dict[str, object]:
    try:
        item = _policy(request).store.request(str(request_id))
    except MCPFailure:
        raise HTTPException(409, "確認要求を再取得してください。") from None
    await answer(request_id, AnswerInput(
        choice=payload.choice, character=item.character_id, session_id=item.session_id,
    ), request, response)
    return {"request": _admin_projection(request, _policy(request).store.request(item.id))}


@router.post("/admin/requests/{request_id}/continue", response_model=None)
async def admin_continue(
    request_id: UUID4, request: Request, response: Response,
) -> PersistedChatResponse | JSONResponse | dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    try:
        item = _policy(request).store.request(str(request_id))
    except MCPFailure:
        raise HTTPException(409, "確認要求を再取得してください。") from None
    if item.key.scene == ExecutionScene.AUTONOMOUS:
        # 会話外は既存runtimeの待機が回答を観測する。別活動を起動しない。
        return {"state": "autonomous"}
    if not _admin_projection(request, item)["connection_available"]:
        return {"state": "ended"}
    manager = getattr(request.app.state, "livekit_runtime_manager", None)
    voice = manager.confirmation_session(item.character_id, item.session_id) if manager else None
    try:
        payload = ContinueInput(
            character=item.character_id, conversation_id=UUID(item.session_id),
            voice_session_id=UUID(voice) if voice else None,
        )
    except ValueError:
        raise HTTPException(409, "この確認要求から会話を続行できません。") from None
    return await continue_conversation(request_id, payload, request, response)
