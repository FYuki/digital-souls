"""会話の外部参照状態と利用者停止。接続設定・生payloadは公開しない。"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, UUID4

from app.routers.validation import ConversationRoute
from app.tool_use.service import ToolService

router = APIRouter(route_class=ConversationRoute)


class StopRequest(BaseModel):
    character: str
    conversation_id: UUID4


@router.get("/tool-use/status/{character}/{conversation_id}")
async def status(
    character: str, conversation_id: UUID4, request: Request
) -> dict[str, object]:
    service: ToolService | None = getattr(request.app.state, "tool_service", None)
    if service is None:
        return {"state": "disabled", "sources": []}
    service.heartbeat(character, str(conversation_id))
    return service.status(character, str(conversation_id))


@router.post("/tool-use/stop")
async def stop(payload: StopRequest, request: Request) -> dict[str, str]:
    service: ToolService | None = getattr(request.app.state, "tool_service", None)
    if service is not None:
        service.stop(payload.character, str(payload.conversation_id))
    return {"state": "stopped"}
