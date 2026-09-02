from fastapi import APIRouter, Request, Response, status

from app.conversation_history.lifecycle_service import ConversationLifecycleService
from app.routers.conversation_contracts import (
    ConversationResponse,
    RenameConversationRequest,
    TurnResponse,
    conversation_response,
    history_turn_response,
)
from app.routers.validation import CanonicalUuid4, ConversationRoute

router = APIRouter(route_class=ConversationRoute)
CONVERSATIONS_PATH = "/characters/{character_id}/conversations"
HARD_DELETE_DESCRIPTION = """
選択したcharacterのconversationと全turnをSQLiteから物理削除します。
削除した短期会話履歴は復元できません。RAG長期記憶は削除されません。
既存のbackup、snapshot、ファイルシステム上の複製からの消去を保証しません。
""".strip()


def _service(request: Request) -> ConversationLifecycleService:
    service: ConversationLifecycleService = (
        request.app.state.conversation_lifecycle_service
    )
    return service


@router.post(
    CONVERSATIONS_PATH,
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(character_id: str, request: Request) -> ConversationResponse:
    return conversation_response(_service(request).create_conversation(character_id))


@router.get(CONVERSATIONS_PATH, response_model=list[ConversationResponse])
def list_active_conversations(
    character_id: str,
    request: Request,
) -> list[ConversationResponse]:
    return [
        conversation_response(item)
        for item in _service(request).list_active_conversations(character_id)
    ]


@router.get(
    f"{CONVERSATIONS_PATH}/archived",
    response_model=list[ConversationResponse],
)
def list_archived_conversations(
    character_id: str,
    request: Request,
) -> list[ConversationResponse]:
    return [
        conversation_response(item)
        for item in _service(request).list_archived_conversations(character_id)
    ]


@router.patch(
    f"{CONVERSATIONS_PATH}/{{conversation_id}}",
    response_model=ConversationResponse,
)
def rename_conversation(
    character_id: str,
    conversation_id: CanonicalUuid4,
    body: RenameConversationRequest,
    request: Request,
) -> ConversationResponse:
    return conversation_response(
        _service(request).rename_conversation(
            character_id,
            conversation_id,
            body.title,
        )
    )


@router.get(
    f"{CONVERSATIONS_PATH}/{{conversation_id}}/turns",
    response_model=list[TurnResponse],
)
def list_conversation_turns(
    character_id: str,
    conversation_id: CanonicalUuid4,
    request: Request,
) -> list[TurnResponse]:
    turns = _service(request).list_conversation_turns(character_id, conversation_id)
    return [history_turn_response(turn) for turn in turns]


@router.post(
    f"{CONVERSATIONS_PATH}/{{conversation_id}}/archive",
    response_model=ConversationResponse,
)
def archive_conversation(
    character_id: str,
    conversation_id: CanonicalUuid4,
    request: Request,
) -> ConversationResponse:
    return conversation_response(
        _service(request).archive_conversation(character_id, conversation_id)
    )


@router.post(
    f"{CONVERSATIONS_PATH}/{{conversation_id}}/unarchive",
    response_model=ConversationResponse,
)
def unarchive_conversation(
    character_id: str,
    conversation_id: CanonicalUuid4,
    request: Request,
) -> ConversationResponse:
    return conversation_response(
        _service(request).unarchive_conversation(character_id, conversation_id)
    )


@router.delete(
    f"{CONVERSATIONS_PATH}/{{conversation_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="短期会話履歴を物理削除",
    description=HARD_DELETE_DESCRIPTION,
)
def hard_delete_conversation(
    character_id: str,
    conversation_id: CanonicalUuid4,
    request: Request,
) -> Response:
    _service(request).hard_delete_conversation(character_id, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
