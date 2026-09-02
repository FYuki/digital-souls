from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app.characters.catalog import (
    CharacterCatalog,
    CharacterCatalogEntry,
    CharacterNotFoundError,
    StandingImageLoadError,
    StandingImageNotConfiguredError,
    StandingImageNotFoundError,
)

router = APIRouter(tags=["characters"])
_catalog = CharacterCatalog(Path(__file__).resolve().parents[3] / "characters")


class StandingImageResponse(BaseModel):
    status: Literal["available", "missing"]
    url: str | None


class CharacterCatalogResponse(BaseModel):
    character_id: str
    display_name: str
    standing_image: StandingImageResponse


def _response(entry: CharacterCatalogEntry) -> CharacterCatalogResponse:
    return CharacterCatalogResponse(
        character_id=entry.character_id,
        display_name=entry.display_name,
        standing_image=StandingImageResponse(
            status=entry.standing_image.status,
            url=entry.standing_image.url,
        ),
    )


def _scan() -> list[CharacterCatalogResponse]:
    return [_response(entry) for entry in _catalog.scan()]


@router.get("/characters", response_model=list[CharacterCatalogResponse])
def list_characters() -> list[CharacterCatalogResponse]:
    return _scan()


@router.post("/characters/rescan", response_model=list[CharacterCatalogResponse])
def rescan_characters() -> list[CharacterCatalogResponse]:
    return _scan()


@router.get("/characters/{character_id}/assets/standing/{filename}")
def get_standing_image(character_id: str, filename: str) -> Response:
    if not filename.endswith(".png"):
        raise HTTPException(
            status_code=404,
            detail={"code": "standing_image_not_found"},
        )
    variant = filename.removesuffix(".png")
    try:
        content = _catalog.load_standing_image(character_id, variant)
    except CharacterNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "character_not_found"},
        ) from error
    except StandingImageNotConfiguredError as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "standing_image_not_configured"},
        ) from error
    except StandingImageNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail={"code": "standing_image_not_found"},
        ) from error
    except StandingImageLoadError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "standing_image_load_failed"},
        ) from error
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )
