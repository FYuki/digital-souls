"""Episode/Fact監査と、Fact単位の訂正・削除API。"""

from typing import Annotated, cast

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.memory.episodic.contracts import FiveW
from app.memory.episodic.management import EpisodicMemoryManagement
from app.routers.memory_management import MemoryManagementRoute
from app.routers.validation import CanonicalUuid4

router = APIRouter(route_class=MemoryManagementRoute)
COLLECTION = "/characters/{character_id}/episodic-memories"


class FactOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: CanonicalUuid4
    expected_version: Annotated[int, Field(strict=True, ge=1)]


class FactCorrectionRequest(FactOperationRequest):
    five_w: FiveW


def _service(request: Request) -> EpisodicMemoryManagement:
    return cast(EpisodicMemoryManagement, request.app.state.episodic_memory_management)


@router.get(COLLECTION)
def list_episodic_memories(character_id: str, request: Request) -> list[dict[str, object]]:
    return _service(request).list(character_id=character_id)


@router.get(f"{COLLECTION}/{{record_id}}")
def get_episodic_memory(character_id: str, record_id: CanonicalUuid4, request: Request) -> dict[str, object]:
    return _service(request).get(character_id=character_id, record_id=record_id)


@router.patch(f"{COLLECTION}/{{record_id}}")
def correct_fact(
    character_id: str, record_id: CanonicalUuid4, correction: FactCorrectionRequest, request: Request,
) -> dict[str, object]:
    service = _service(request)
    service.correct(character_id=character_id, record_id=record_id,
                    expected_version=correction.expected_version, five_w=correction.five_w,
                    idempotency_key=correction.idempotency_key)
    return service.get(character_id=character_id, record_id=record_id)


@router.delete(f"{COLLECTION}/{{record_id}}", status_code=204)
def delete_fact(
    character_id: str, record_id: CanonicalUuid4, operation: FactOperationRequest, request: Request,
) -> Response:
    _service(request).delete(character_id=character_id, record_id=record_id,
                             expected_version=operation.expected_version,
                             idempotency_key=operation.idempotency_key)
    return Response(status_code=204)
