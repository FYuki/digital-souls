"""キャラクター単位の中期状態・自律許可・実行管理。接続credentialは受け取らない。"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field, UUID4

from app.characters.loader import load_character_card
from app.external_mcp.models import MCPFailure
from app.routers.validation import SafeValidationRoute
from app.character_life.models import (
    Content,
    Identifier,
    Kind,
    LifeError,
    LifeState,
    Record,
    Result,
    StateStatus,
)
from app.character_life.runtime import Runtime


def runtime(request: Request) -> Runtime:
    value = getattr(request.app.state, "character_life_runtime", None)
    if value is None or not value.started:
        raise HTTPException(503, detail={"code": "character_life_disabled"})
    return cast(Runtime, value)


def secure_character(character: str, request: Request, response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    if request.method != "GET":
        if (
            request.headers.get("origin")
            != request.app.state.screen_http_security.allowed_origin
        ):
            raise HTTPException(403, detail={"code": "origin_mismatch"})
    try:
        load_character_card(character)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, detail={"code": "character_not_found"}) from None


router = APIRouter(
    prefix="/character-life/{character}",
    route_class=SafeValidationRoute,
    dependencies=[Depends(secure_character)],
)


class StateInput(Record):
    kind: Kind
    content: Content
    target_id: Identifier | None = None
    binding_target_id: Identifier | None = None


class StateUpdate(Record):
    target_id: Identifier | None = None
    binding_target_id: Identifier | None = None
    revision: int = Field(ge=1)
    content: Content
    status: StateStatus


class GrantInput(Record):
    enabled: bool


class ActivityInput(Record):
    state_id: UUID4
    request_id: UUID4


def error(value: LifeError) -> HTTPException:
    return HTTPException(
        409
        if value.result in {Result.CONFLICT, Result.SUPERSEDED}
        else 503
        if value.result is Result.DEFERRED
        else 400,
        detail={"result": value.result, "code": value.reason},
    )


def shared_target(rt: Runtime, character: str, target_id: str) -> None:
    try:
        policy = rt.service.gate.registry.entry(target_id).connection.manifest[
            "core_policy"
        ]["sharing"]
    except MCPFailure:
        raise HTTPException(404, detail={"code": "target_not_registered"}) from None
    if policy["mode"] == "user_bound" or (
        policy["mode"] == "character_bound" and policy["character_id"] != character
    ):
        raise HTTPException(403, detail={"code": "target_not_shared"})


@router.get("")
def status(character: str, request: Request) -> dict[str, object]:
    rt = runtime(request)
    return {
        "enabled": True,
        "states": rt.service.store.states(character),
        "grants": [
            {
                "connection_id": g.connection_id,
                "enabled": g.enabled,
                "revision": g.revision,
            }
            for g in rt.service.store.grants(character)
        ],
        "runs": rt.service.store.runs(character),
        "formation_jobs": rt.service.store.formation_jobs(character),
        "dependencies": {
            "episode_reflection": "deferred_issue_100",
            "personality": "deferred_issue_101",
            "write_recovery": "deferred_issue_185",
            "skill": "deferred_issue_102",
        },
    }


@router.post("/states", status_code=201)
async def create_state(character: str, body: StateInput, request: Request) -> LifeState:
    rt = runtime(request)
    try:
        if not await rt.service.privacy.allowed(body.content):
            raise HTTPException(400, detail={"code": "state_privacy_blocked"})
    except LifeError as exc:
        raise error(exc) from None
    if body.target_id is not None:
        shared_target(rt, character, body.target_id)
    state = LifeState(
        character_id=character,
        kind=body.kind,
        content=body.content,
        target_id=body.target_id,
        binding_target_id=body.binding_target_id,
        source="user",
    )
    return rt.service.store.save_state(state)


@router.put("/states/{state_id}")
async def update_state(
    character: str, state_id: UUID, body: StateUpdate, request: Request
) -> LifeState:
    rt = runtime(request)
    try:
        state = rt.service.store.state(character, state_id)
        if not await rt.service.privacy.allowed(body.content):
            raise HTTPException(400, detail={"code": "state_privacy_blocked"})
        if body.target_id is not None:
            shared_target(rt, character, body.target_id)
        return rt.service.store.save_state(
            state.model_copy(
                update={
                    "content": body.content,
                    "status": body.status,
                    "target_id": body.target_id
                    if "target_id" in body.model_fields_set
                    else state.target_id,
                    "binding_target_id": body.binding_target_id
                    if "binding_target_id" in body.model_fields_set
                    else state.binding_target_id,
                    **(
                        {"source": "user", "source_ids": (), "reflection_revisions": {}}
                        if body.content != state.content
                        else {}
                    ),
                }
            ),
            expected_revision=body.revision,
        )
    except LifeError as exc:
        raise error(exc) from None


@router.put("/targets/{connection_id}")
async def grant(
    character: str, connection_id: str, body: GrantInput, request: Request
) -> dict[str, object]:
    rt = runtime(request)
    try:
        if not body.enabled:
            try:
                previous = rt.service.store.grant(character, connection_id)
            except LifeError:
                previous = None
            if previous is not None:
                value = rt.service.store.set_grant(
                    character, connection_id, previous.connection_identity, False
                )
                return {
                    "connection_id": value.connection_id,
                    "enabled": value.enabled,
                    "revision": value.revision,
                }
        shared_target(rt, character, connection_id)
        connection = rt.service.gate.registry.entry(connection_id).connection
        value = rt.service.store.set_grant(
            character, connection_id, connection.identity, body.enabled
        )
        return {
            "connection_id": value.connection_id,
            "enabled": value.enabled,
            "revision": value.revision,
        }
    except MCPFailure:
        raise HTTPException(404, detail={"code": "target_not_registered"}) from None


@router.post("/activities", status_code=202)
async def submit(character: str, body: ActivityInput, request: Request) -> object:
    rt = runtime(request)
    try:
        return await rt.submit(
            character, body.state_id, str(body.request_id), requested=True
        )
    except LifeError as exc:
        raise error(exc) from None


def owned_run(rt: Runtime, character: str, run_id: UUID) -> str:
    try:
        run = rt.service.store.run(str(run_id))
        if run.character_id != character:
            raise LifeError(Result.REJECTED, "run_not_found")
        return str(run_id)
    except LifeError:
        raise HTTPException(404, detail={"code": "run_not_found"}) from None


@router.post("/activities/{run_id}/pause")
async def pause(character: str, run_id: UUID, request: Request) -> object:
    rt = runtime(request)
    key = owned_run(rt, character, run_id)
    rt.service.pause(key)
    return rt.service.store.run(key)


@router.post("/activities/{run_id}/resume")
async def resume(character: str, run_id: UUID, request: Request) -> object:
    rt = runtime(request)
    key = owned_run(rt, character, run_id)
    try:
        return await rt.resume(key)
    except LifeError as exc:
        raise error(exc) from None


class FormationInput(Record):
    request_id: UUID4


@router.post("/formation", status_code=202)
async def form(
    character: str, body: FormationInput, request: Request
) -> dict[str, str]:
    try:
        workflow_id = await runtime(request).submit_formation(
            character, str(body.request_id)
        )
    except LifeError as exc:
        raise error(exc) from None
    return {"workflow_id": workflow_id, "dependency": "issue_100"}


@router.get("/states/{state_id}/history")
def history(character: str, state_id: UUID, request: Request) -> object:
    return runtime(request).service.store.state_history(character, state_id)


@router.get("/audit")
def audit(
    character: str, request: Request, after: int = Query(default=0, ge=0)
) -> object:
    return runtime(request).service.store.audit(character, after=after)
