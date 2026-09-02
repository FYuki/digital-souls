import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.characters.catalog import CharacterCatalog
from app.routers import character_catalog

PNG = b"\x89PNG\r\n\x1a\nimage"


def _write_character(root: Path, character_id: str, name: str) -> Path:
    directory = root / character_id
    directory.mkdir(parents=True)
    card = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": name,
            "description": "description",
            "personality": "personality",
            "scenario": "scenario",
            "first_mes": "hello",
            "mes_example": "example",
            "system_prompt": "prompt",
        },
    }
    (directory / f"{character_id}.card.json").write_text(
        json.dumps(card), encoding="utf-8"
    )
    return directory


def _client(monkeypatch, root: Path) -> TestClient:
    monkeypatch.setattr(character_catalog, "_catalog", CharacterCatalog(root))
    app = FastAPI()
    app.include_router(character_catalog.router)
    return TestClient(app)


def test_catalog_get_and_explicit_rescan_both_read_current_disk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "characters"
    _write_character(root, "miori", "光織")

    with _client(monkeypatch, root) as client:
        assert [entry["display_name"] for entry in client.get("/characters").json()] == [
            "光織"
        ]
        _write_character(root, "new-character", "新規")
        response = client.post("/characters/rescan")

    assert response.status_code == 200
    assert [entry["character_id"] for entry in response.json()] == [
        "miori",
        "new-character",
    ]


def test_default_standing_image_is_served_as_png(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "characters"
    character = _write_character(root, "miori", "光織")
    standing = character / "assets" / "standing"
    standing.mkdir(parents=True)
    (standing / "default.png").write_bytes(PNG)

    with _client(monkeypatch, root) as client:
        response = client.get(
            "/characters/miori/assets/standing/default.png"
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == PNG


def test_asset_errors_have_distinct_machine_readable_codes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "characters"
    character = _write_character(root, "miori", "光織")
    standing = character / "assets" / "standing"
    standing.mkdir(parents=True)
    (standing / "default.png").write_bytes(b"broken")

    with _client(monkeypatch, root) as client:
        unknown = client.get(
            "/characters/unknown/assets/standing/default.png"
        )
        missing = client.get(
            "/characters/miori/assets/standing/night.png"
        )
        load_failed = client.get(
            "/characters/miori/assets/standing/default.png"
        )

    assert unknown.json()["detail"]["code"] == "character_not_found"
    assert missing.json()["detail"]["code"] == "standing_image_not_found"
    assert load_failed.status_code == 503
    assert load_failed.json()["detail"]["code"] == "standing_image_load_failed"


def test_unconfigured_default_is_distinct_from_missing_variant(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "characters"
    _write_character(root, "miori", "光織")

    with _client(monkeypatch, root) as client:
        response = client.get(
            "/characters/miori/assets/standing/default.png"
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == (
        "standing_image_not_configured"
    )
