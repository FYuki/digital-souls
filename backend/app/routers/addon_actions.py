"""内部管理・チャットからの承認回答。回答自体は外部操作を実行しない。"""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, UUID4

from app.addon_action.models import ApprovalChoice
from app.addon_action.policy import ActionPolicy
from app.external_mcp.models import MCPFailure
from app.routers.validation import SafeValidationRoute

router = APIRouter(prefix="/addon-actions", route_class=SafeValidationRoute)


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: ApprovalChoice
    character: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)


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
