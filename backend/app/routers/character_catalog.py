from hashlib import sha256
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
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


def catalog_contains(character_id: str) -> bool:
    return any(
        entry.character_id == character_id
        for entry in _catalog.scan()
    )


@router.get("/characters", response_model=list[CharacterCatalogResponse])
def list_characters() -> list[CharacterCatalogResponse]:
    return _scan()


@router.post("/characters/rescan", response_model=list[CharacterCatalogResponse])
def rescan_characters() -> list[CharacterCatalogResponse]:
    return _scan()


def _image_headers(content: bytes) -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "ETag": f'"{sha256(content).hexdigest()}"',
        "X-Content-Type-Options": "nosniff",
    }


def _matches_etag(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    return any(
        candidate == "*" or candidate.removeprefix("W/") == etag
        for candidate in (
            raw_candidate.strip() for raw_candidate in if_none_match.split(",")
        )
    )


@router.get("/characters/{character_id}/assets/standing/{filename}")
def get_standing_image(
    character_id: str,
    filename: str,
    request: Request,
) -> Response:
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
    headers = _image_headers(content)
    if _matches_etag(request.headers.get("if-none-match"), headers["ETag"]):
        return Response(status_code=304, headers=headers)
    return Response(
        content=content,
        media_type="image/png",
        headers=headers,
    )
