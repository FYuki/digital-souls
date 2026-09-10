"""HTTP境界のOrigin・キャラクター分離・競合・秘密非反射を確認する。"""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers import character_life
from tests.character_life_test_support import environment


def test_state_management_origin_conflict_and_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(character_life, "load_character_card", lambda character: None)

    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            app = FastAPI()
            app.include_router(character_life.router)
            app.state.screen_http_security = SimpleNamespace(
                allowed_origin="http://localhost:5173"
            )
            app.state.character_life_runtime = SimpleNamespace(
                started=True, service=service
            )
            async with AsyncClient(
                transport=ASGITransport(app), base_url="http://localhost"
            ) as client:
                base = "/character-life/miori"
                body = {"kind": "INTEREST", "content": "色彩への関心"}
                assert (
                    await client.post(base + "/states", json=body)
                ).status_code == 403
                client.headers["Origin"] = "http://localhost:5173"
                created = await client.post(base + "/states", json=body)
                assert created.status_code == 201
                assert created.headers["Cache-Control"] == "no-store"
                state = created.json()
                update = {
                    "content": "絵画への関心",
                    "status": "ACTIVE",
                    "revision": state["revision"],
                }
                url = base + "/states/" + state["id"]
                assert (
                    await client.put(url, json={**update, "target_id": "unknown"})
                ).status_code == 404
                assert (await client.put(url, json=update)).status_code == 200
                assert (await client.put(url, json=update)).status_code == 409
                assert (
                    await client.put(url.replace("miori", "other"), json=update)
                ).status_code == 400
                history = (await client.get(url + "/history")).json()
                assert [s["revision"] for s in history] == [1, 2]
                assert (
                    await client.get(url.replace("miori", "other") + "/history")
                ).json() == []
                paused = await client.post(base + f"/activities/{run.id}/pause")
                assert paused.json()["phase"] == "paused"
                save = service.store.save_run

                def resume_before_save(stale, **kwargs):
                    current = service.store.run(str(stale.id))
                    save(current.model_copy(update={
                        "attempt": current.attempt + 1, "phase": "queued"
                    }), expected_attempt=current.attempt)
                    return save(stale, **kwargs)

                with monkeypatch.context() as patch:
                    patch.setattr(service.store, "save_run", resume_before_save)
                    conflict = await client.post(base + f"/activities/{run.id}/pause")
                assert conflict.status_code == 409
                assert conflict.json() == {"detail": {
                    "result": "CONFLICT", "code": "attempt_superseded"
                }}
                assert (
                    await client.post(
                        f"/character-life/other/activities/{run.id}/pause"
                    )
                ).status_code == 404
                assert (await client.get(base + "/audit")).json()
                assert (await client.get("/character-life/other/audit")).json() == []
                invalid = await client.post(
                    base + "/states", json={**body, "api_key": "PRIVATE_SENTINEL"}
                )
                assert invalid.status_code == 422
                assert "PRIVATE_SENTINEL" not in invalid.text
                assert source.calls == []

    asyncio.run(scenario())


def test_unregistered_character_cannot_create_state(tmp_path, monkeypatch):
    def missing_character(character):
        raise FileNotFoundError("synthetic private path")
    monkeypatch.setattr(character_life, "load_character_card", missing_character)

    async def scenario():
        async with environment(tmp_path) as (service, _, _):
            app = FastAPI()
            app.include_router(character_life.router)
            app.state.screen_http_security = SimpleNamespace(allowed_origin="http://localhost:5173")
            app.state.character_life_runtime = SimpleNamespace(started=True, service=service)
            async with AsyncClient(
                transport=ASGITransport(app), base_url="http://localhost",
                headers={"Origin": "http://localhost:5173"},
            ) as client:
                response = await client.post(
                    "/character-life/missing/states",
                    json={"kind": "INTEREST", "content": "保存されない関心"},
                )
            assert response.status_code == 404
            assert response.json() == {"detail": {"code": "character_not_found"}}
            assert service.store.states("missing") == []
    asyncio.run(scenario())
