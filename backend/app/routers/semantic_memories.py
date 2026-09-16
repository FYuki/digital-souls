"""意味記憶監査と自己申告の訂正・削除API。"""

from typing import Annotated, cast

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.memory.semantic.management import SemanticMemoryManagement
from app.routers.memory_management import MemoryManagementRoute
from app.routers.validation import CanonicalUuid4

router = APIRouter(route_class=MemoryManagementRoute)
COLLECTION = "/characters/{character_id}/semantic-memories"


class SemanticDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Annotated[int, Field(strict=True, ge=1)]


class SemanticCorrectionRequest(SemanticDeleteRequest):
    idempotency_key: CanonicalUuid4
    value: Annotated[str, Field(strict=True, min_length=1, max_length=240, pattern=r"\S")]


def _service(request: Request) -> SemanticMemoryManagement:
    return cast(SemanticMemoryManagement, request.app.state.semantic_memory_management)


@router.get(COLLECTION)
def list_semantic_memories(character_id: str, request: Request) -> list[dict[str, object]]:
    return _service(request).list(character_id=character_id)


@router.get(f"{COLLECTION}/{{record_id}}")
def get_semantic_memory(character_id: str, record_id: CanonicalUuid4, request: Request) -> dict[str, object]:
    return _service(request).get(character_id=character_id, record_id=record_id)


@router.patch(f"{COLLECTION}/{{record_id}}")
def correct_semantic_memory(
    character_id: str, record_id: CanonicalUuid4, correction: SemanticCorrectionRequest, request: Request,
) -> dict[str, object]:
    return _service(request).correct(character_id=character_id, record_id=record_id,
        version=correction.expected_version, value=correction.value.strip(), key=correction.idempotency_key)


@router.delete(f"{COLLECTION}/{{record_id}}", status_code=204)
def delete_semantic_memory(
    character_id: str, record_id: CanonicalUuid4, operation: SemanticDeleteRequest, request: Request,
) -> Response:
    _service(request).delete(character_id=character_id, record_id=record_id, version=operation.expected_version)
    return Response(status_code=204)
