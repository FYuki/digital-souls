"""登録済み接続の一覧と希望ON/OFFだけを公開する。"""

from collections.abc import Callable, Coroutine
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from app.addon_admin.runtime import AddonRuntime
from app.external_mcp.models import MCPFailure


class ManagementRoute(APIRoute):
    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[object, object, Response]]:
        handler = super().get_route_handler()

        async def safe(request: Request) -> Response:
            try:
                response = await handler(request)
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


def manager(request: Request) -> AddonRuntime:
    runtime = getattr(request.app.state, "addon_manager", None)
    if runtime is None:
        raise HTTPException(503, "management_unavailable")
    return cast(AddonRuntime, runtime)


@router.get("/connections", response_model=list[AddonStatus])
async def connections(request: Request) -> list[dict[str, object]]:
    return manager(request).list()


@router.patch("/connections/{connection_id}", response_model=AddonStatus)
async def enabled(
    connection_id: str, payload: EnabledPatch, request: Request
) -> dict[str, object]:
    try:
        return manager(request).set_enabled(connection_id, payload.desired_enabled)
    except MCPFailure:
        raise HTTPException(404, "connection_not_found") from None
    except OSError:
        raise HTTPException(503, "settings_save_failed") from None
