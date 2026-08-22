from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.memory.admission.contracts import (
    EpisodicEventType,
    EpisodicEventValue,
    EpisodicSubject,
    InteractionAspect,
    InteractionPreferenceValue,
    MemoryCandidate,
    MemoryType,
    PreferencePolarity,
    UserPreferenceValue,
)
from app.memory.persistence.contracts import (
    ApprovedMemory,
    TemporaryProviderRecord,
    TemporaryProviderRecordCorrection,
)
from app.memory.providers import MemoryCorrectionRejected
from app.memory.persistence.sqlite import format_datetime
from app.routers.validation import CanonicalUuid4, SafeValidationRoute


class MemoryManagementRoute(SafeValidationRoute):
    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        route_handler = super().get_route_handler()

        async def memory_route_handler(request: Request) -> Response:
            try:
                return await route_handler(request)
            except MemoryCorrectionRejected as error:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={"reason_code": error.reason_code},
                )
            except LookupError as error:
                raise HTTPException(status_code=404, detail="record was not found") from error
            except (TypeError, ValueError) as error:
                raise HTTPException(status_code=422, detail="invalid memory request") from error

        return memory_route_handler


router = APIRouter(route_class=MemoryManagementRoute)
PERSONA_COLLECTION = "/characters/{character_id}/persona-memories"
TEMPORARY_COLLECTION = "/characters/{character_id}/temporary-records/{provider_id}"


class PersonaCorrectionRequest(BaseModel):
    idempotency_key: CanonicalUuid4
    memory_type: MemoryType
    structured_value: dict[str, object]

    def to_candidate(self) -> MemoryCandidate:
        value = self.structured_value
        if self.memory_type is MemoryType.EPISODIC_EVENT:
            structured = EpisodicEventValue(
                event_type=EpisodicEventType(str(value.get("event_type", ""))),
                subject=EpisodicSubject(str(value.get("subject", ""))),
                topic=_required_string(value, "topic"),
            )
        elif self.memory_type is MemoryType.USER_PREFERENCE:
            alternative = value.get("alternative")
            structured = UserPreferenceValue(
                polarity=PreferencePolarity(str(value.get("polarity", ""))),
                object=_required_string(value, "object"),
                alternative=(
                    None if alternative is None else _required_string(value, "alternative")
                ),
            )
        else:
            structured = InteractionPreferenceValue(
                aspect=InteractionAspect(str(value.get("aspect", ""))),
                value=_required_string(value, "value"),
            )
        return MemoryCandidate(
            memory_type=self.memory_type, structured_value=structured, source=None
        )


class TemporaryCorrectionRequest(BaseModel):
    record_type: str
    structured_value: str
    effective_at: datetime

    def to_correction(self) -> TemporaryProviderRecordCorrection:
        return TemporaryProviderRecordCorrection(
            record_type=self.record_type,
            structured_value=self.structured_value,
            effective_at=self.effective_at,
        )


def _required_string(values: dict[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _persona_provider(request: Request):
    return request.app.state.persona_memory_provider


def _addon_provider(request: Request):
    return request.app.state.addon_record_provider


def _validate_temporary_provider(provider_id: str) -> None:
    if provider_id not in {"temporary:agriculture", "temporary:recipe"}:
        raise ValueError("provider_id must be a temporary provider")


@router.get(PERSONA_COLLECTION)
def list_persona_memories(
    character_id: str, request: Request, status: str = "ACTIVE"
) -> list[object]:
    return _persona_provider(request).list(character_id=character_id, status=status)


@router.get(f"{PERSONA_COLLECTION}/{{memory_id}}")
def get_persona_memory(
    character_id: str, memory_id: CanonicalUuid4, request: Request
) -> object:
    result = _persona_provider(request).get(
        character_id=character_id, memory_id=memory_id
    )
    if result is None:
        raise LookupError("approved memory was not found")
    return result


@router.patch(f"{PERSONA_COLLECTION}/{{memory_id}}")
def correct_persona_memory(
    character_id: str,
    memory_id: CanonicalUuid4,
    correction: PersonaCorrectionRequest,
    request: Request,
) -> object:
    provider = _persona_provider(request)
    result = provider.correct(
        character_id=character_id,
        memory_id=memory_id,
        candidate=correction.to_candidate(),
        idempotency_key=correction.idempotency_key,
    )
    if isinstance(result, ApprovedMemory):
        detail = provider.get(character_id=character_id, memory_id=memory_id)
        if detail is None:
            raise RuntimeError("corrected memory could not be read")
        return detail
    return result


@router.delete(
    f"{PERSONA_COLLECTION}/{{memory_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_persona_memory(
    character_id: str, memory_id: CanonicalUuid4, request: Request
) -> Response:
    _persona_provider(request).hard_delete(
        character_id=character_id, memory_id=memory_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(TEMPORARY_COLLECTION)
def list_temporary_records(
    character_id: str, provider_id: str, request: Request
) -> list[object]:
    _validate_temporary_provider(provider_id)
    records = _addon_provider(request).list(
        character_id=character_id, provider_id=provider_id
    )
    return [_temporary_response(record) for record in records]


@router.get(f"{TEMPORARY_COLLECTION}/{{record_id}}")
def get_temporary_record(
    character_id: str,
    provider_id: str,
    record_id: CanonicalUuid4,
    request: Request,
) -> object:
    _validate_temporary_provider(provider_id)
    result = _addon_provider(request).get(
        character_id=character_id, provider_id=provider_id, record_id=record_id
    )
    if result is None:
        raise LookupError("temporary record was not found")
    return _temporary_response(result)


@router.patch(f"{TEMPORARY_COLLECTION}/{{record_id}}")
def correct_temporary_record(
    character_id: str,
    provider_id: str,
    record_id: CanonicalUuid4,
    correction: TemporaryCorrectionRequest,
    request: Request,
) -> object:
    _validate_temporary_provider(provider_id)
    result = _addon_provider(request).correct(
        character_id=character_id,
        provider_id=provider_id,
        record_id=record_id,
        correction=correction.to_correction(),
    )
    return _temporary_response(result)


@router.delete(
    f"{TEMPORARY_COLLECTION}/{{record_id}}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_temporary_record(
    character_id: str,
    provider_id: str,
    record_id: CanonicalUuid4,
    request: Request,
) -> Response:
    _validate_temporary_provider(provider_id)
    _addon_provider(request).hard_delete(
        character_id=character_id, provider_id=provider_id, record_id=record_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _temporary_response(record: object) -> object:
    if not isinstance(record, TemporaryProviderRecord):
        return record
    return {
        "id": record.id,
        "character_id": record.character_id,
        "provider_id": record.provider_id,
        "source_ref": record.source_ref,
        "record_type": record.record_type,
        "structured_value": record.structured_value,
        "effective_at": format_datetime(record.effective_at),
        "updated_at": format_datetime(record.updated_at),
    }
