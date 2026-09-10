"""接続の希望状態とExternal MCP専用の設定管理を公開する。"""

from collections.abc import Callable, Coroutine
import sqlite3
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from app.addon_admin.runtime import AddonRuntime
from app.addon_admin.management import ConnectionManagement
from app.addon_admin.connection_models import ConnectionInput, CredentialInput
from app.addon_admin.store import SettingsDurabilityError
from app.external_mcp.models import MCPFailure


class ManagementRoute(APIRoute):
    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[object, object, Response]]:
        handler = super().get_route_handler()

        async def safe(request: Request) -> Response:
            try:
                response = await handler(request)
            except MCPFailure as error:
                code = error.code
                if code == "unknown_connection":
                    raise HTTPException(404, "connection_not_found") from None
                if code in {
                    "connection_busy",
                    "connection_changed",
                    "connection_unconfirmed",
                }:
                    raise HTTPException(409, code) from None
                if code in {
                    "external_connection_required",
                    "credential_not_applicable",
                }:
                    raise HTTPException(422, code) from None
                raise HTTPException(422, "invalid_connection_settings") from None
            except (sqlite3.Error, OSError):
                raise HTTPException(503, "connection_save_failed") from None
            except RequestValidationError:
                # 未知field名や送信JSONにもsecretが入り得るため、詳細を反射しない。
                raise HTTPException(422, "invalid_management_request") from None
            response.headers["Cache-Control"] = "no-store"
            return response

        return safe


router = APIRouter(prefix="/addon-admin", route_class=ManagementRoute)


class EnabledPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    desired_enabled: bool


class AddonStatus(BaseModel):
    connection_instance_id: str
    display_name: str
    source_type: Literal["external", "self_owned"]
    desired_enabled: bool
    availability: Literal["unknown", "available", "degraded", "unavailable"]
    effective_state: Literal[
        "disabled", "unknown", "available", "degraded", "unavailable"
    ]
    error_code: (
        Literal[
            "connection_failed",
            "authentication_failed",
            "protocol_error",
            "health_check_failed",
            "partial_failure",
        ]
        | None
    )
    last_checked_at: str | None
    settings_revision: int | None = None
    last_success_at: str | None = None
    last_attempt_at: str | None = None
    last_check_error: str | None = None


def manager(request: Request) -> AddonRuntime:
    runtime = getattr(request.app.state, "addon_manager", None)
    if runtime is None:
        raise HTTPException(503, "management_unavailable")
    return cast(AddonRuntime, runtime)


@router.get(
    "/connections", response_model=list[AddonStatus], response_model_exclude_unset=True
)
async def connections(request: Request) -> list[dict[str, object]]:
    return manager(request).list()


@router.patch(
    "/connections/{connection_id}",
    response_model=AddonStatus,
    response_model_exclude_unset=True,
)
async def enabled(
    connection_id: str, payload: EnabledPatch, request: Request
) -> dict[str, object]:
    runtime = manager(request)
    try:
        if isinstance(runtime, ConnectionManagement):
            return await runtime.enable(connection_id, payload.desired_enabled)
        return runtime.set_enabled(connection_id, payload.desired_enabled)
    except MCPFailure:
        # 管理拡張の競合・未確認エラーも共通の安全な変換へ渡す。
        raise
    except SettingsDurabilityError:
        raise HTTPException(503, "settings_durability_uncertain") from None
    except OSError:
        raise HTTPException(503, "settings_save_failed") from None


def connection_manager(request: Request) -> ConnectionManagement:
    runtime = manager(request)
    if not isinstance(runtime, ConnectionManagement):
        raise HTTPException(503, "management_unavailable")
    return runtime


@router.post("/external-connections", status_code=201)
async def create_connection(
    payload: ConnectionInput, request: Request
) -> dict[str, object]:
    return await connection_manager(request).create(payload)


@router.get("/external-connections/{connection_id}")
async def connection_detail(connection_id: str, request: Request) -> dict[str, object]:
    return connection_manager(request).detail(connection_id)


@router.put("/external-connections/{connection_id}")
async def update_connection(
    connection_id: str, payload: ConnectionInput, request: Request
) -> dict[str, object]:
    return await connection_manager(request).update(connection_id, payload)


@router.put("/external-connections/{connection_id}/credential")
async def update_credential(
    connection_id: str, payload: CredentialInput, request: Request
) -> dict[str, object]:
    return await connection_manager(request).credential(connection_id, payload)


@router.post("/external-connections/{connection_id}/check")
async def check_connection(connection_id: str, request: Request) -> dict[str, object]:
    return await connection_manager(request).check(connection_id)


@router.delete("/external-connections/{connection_id}", status_code=204)
async def delete_connection(connection_id: str, request: Request) -> Response:
    await connection_manager(request).delete(connection_id)
    return Response(status_code=204)
