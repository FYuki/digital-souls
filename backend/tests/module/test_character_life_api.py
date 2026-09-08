"""HTTP境界のOrigin・キャラクター分離・競合・秘密非反射を確認する。"""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers import character_life
from tests.module.test_character_life import environment


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
