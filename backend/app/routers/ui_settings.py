from typing import Annotated, Literal, cast

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, model_validator

from app.routers import character_catalog
from app.routers.validation import CanonicalUuid4
from app.ui_settings import UiSettingsRepository, UiSettingsSnapshot
from app.ui_settings.errors import (
    UiCharacterNotAddedError,
    UiThreadNotFoundError,
)
from app.ui_settings.models import PortraitLayout

router = APIRouter(prefix="/ui-settings", tags=["ui-settings"])
LOCAL_USER_ID = "local"
CharacterId = Annotated[
    str,
    Path(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"),
]


class UiPreferencesPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    desktop_portrait_layout: Literal["right", "background"] | None = None
    desktop_history_height_percent: Literal[50, 75, 100] | None = None
    compact_history_height_percent: Literal[50, 75, 100] | None = None

    @model_validator(mode="after")
    def require_update(self) -> "UiPreferencesPatch":
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("at least one preference is required")
        return self


class CharacterVisibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    visible: bool


class CharacterUiStateResponse(BaseModel):
    character_id: str
    visible: bool
    pinned: bool
    pin_order: int | None


class ThreadPinResponse(BaseModel):
    character_id: str
    conversation_id: str


class UiSettingsResponse(BaseModel):
    user_id: str
    desktop_portrait_layout: Literal["right", "background"]
    desktop_history_height_percent: Literal[50, 75, 100]
    compact_history_height_percent: Literal[50, 75, 100]
    characters: list[CharacterUiStateResponse]
    thread_pins: list[ThreadPinResponse]


def _repository(request: Request) -> UiSettingsRepository:
    repository: UiSettingsRepository = request.app.state.ui_settings_repository
    return repository


def _response(snapshot: UiSettingsSnapshot) -> UiSettingsResponse:
    return UiSettingsResponse(
        user_id=snapshot.user_id,
        desktop_portrait_layout=snapshot.preferences.desktop_portrait_layout.value,
        desktop_history_height_percent=(
            cast(
                Literal[50, 75, 100],
                snapshot.preferences.desktop_history_height_percent,
            )
        ),
        compact_history_height_percent=(
            cast(
                Literal[50, 75, 100],
                snapshot.preferences.compact_history_height_percent,
            )
        ),
        characters=[
            CharacterUiStateResponse(
                character_id=item.character_id,
                visible=item.visible,
                pinned=item.pin_order is not None,
                pin_order=item.pin_order,
            )
            for item in snapshot.characters
        ],
        thread_pins=[
            ThreadPinResponse(
                character_id=item.character_id,
                conversation_id=str(item.conversation_id),
            )
            for item in snapshot.thread_pins
        ],
    )


@router.get("", response_model=UiSettingsResponse)
def get_ui_settings(request: Request) -> UiSettingsResponse:
    return _response(_repository(request).get(LOCAL_USER_ID))


@router.patch("", response_model=UiSettingsResponse)
def update_ui_preferences(
    body: UiPreferencesPatch,
    request: Request,
) -> UiSettingsResponse:
    return _response(
        _repository(request).update_preferences(
            LOCAL_USER_ID,
            desktop_portrait_layout=(
                None
                if body.desktop_portrait_layout is None
                else PortraitLayout(body.desktop_portrait_layout)
            ),
            desktop_history_height_percent=(
                body.desktop_history_height_percent
            ),
            compact_history_height_percent=(
                body.compact_history_height_percent
            ),
        )
    )


@router.put("/characters/{character_id}", response_model=UiSettingsResponse)
def set_character_visibility(
    character_id: CharacterId,
    body: CharacterVisibilityRequest,
    request: Request,
) -> UiSettingsResponse:
    if body.visible and not character_catalog.catalog_contains(character_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "character_not_found"},
        )
    try:
        snapshot = _repository(request).set_character_visibility(
            LOCAL_USER_ID, character_id, visible=body.visible
        )
    except UiCharacterNotAddedError as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "ui_character_not_added"},
        ) from error
    return _response(snapshot)


def _set_character_pin(
    request: Request,
    character_id: str,
    *,
    pinned: bool,
) -> UiSettingsResponse:
    try:
        snapshot = _repository(request).set_character_pinned(
            LOCAL_USER_ID,
            character_id,
            pinned=pinned,
        )
    except UiCharacterNotAddedError as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "ui_character_not_added"},
        ) from error
    return _response(snapshot)


@router.put("/characters/{character_id}/pin", response_model=UiSettingsResponse)
def pin_character(
    character_id: CharacterId,
    request: Request,
) -> UiSettingsResponse:
    return _set_character_pin(request, character_id, pinned=True)


@router.delete("/characters/{character_id}/pin", response_model=UiSettingsResponse)
def unpin_character(
    character_id: CharacterId,
    request: Request,
) -> UiSettingsResponse:
    return _set_character_pin(request, character_id, pinned=False)


def _set_thread_pin(
    request: Request,
    character_id: str,
    conversation_id: CanonicalUuid4,
    *,
    pinned: bool,
) -> UiSettingsResponse:
    try:
        snapshot = _repository(request).set_thread_pinned(
            LOCAL_USER_ID,
            character_id,
            conversation_id,
            pinned=pinned,
        )
    except UiCharacterNotAddedError as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "ui_character_not_added"},
        ) from error
    except UiThreadNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "ui_thread_not_found"},
        ) from error
    return _response(snapshot)


THREAD_PIN_PATH = "/characters/{character_id}/conversations/{conversation_id}/pin"


@router.put(THREAD_PIN_PATH, response_model=UiSettingsResponse)
def pin_thread(
    character_id: CharacterId,
    conversation_id: CanonicalUuid4,
    request: Request,
) -> UiSettingsResponse:
    return _set_thread_pin(
        request,
        character_id,
        conversation_id,
        pinned=True,
    )


@router.delete(THREAD_PIN_PATH, response_model=UiSettingsResponse)
def unpin_thread(
    character_id: CharacterId,
    conversation_id: CanonicalUuid4,
    request: Request,
) -> UiSettingsResponse:
    return _set_thread_pin(
        request,
        character_id,
        conversation_id,
        pinned=False,
    )
