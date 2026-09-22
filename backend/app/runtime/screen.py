"""Screen PerceptionのHTTP securityとService公開を所有する。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING
from uuid import UUID

from app.screen_perception.http_security import (
    SCREEN_ALLOWED_ORIGIN_ENV,
    ScreenHttpSecurity,
    resolve_screen_http_security,
)
from app.screen_perception.service import (
    ScreenPerceptionService,
    resolve_routing_policy,
)
from app.screen_perception.vision import VisionInferenceClient

if TYPE_CHECKING:
    from fastapi import FastAPI
    from app.runtime.inference import InferenceResources

SCREEN_HTTP_SECURITY_DEFAULT_ORIGIN = "http://localhost:5173"


class ScreenResources:
    """Screen Perceptionのsecurity設定とServiceを所有する資源owner。"""

    def __init__(self, security: ScreenHttpSecurity) -> None:
        self.security = security
        self.service: ScreenPerceptionService | None = None
        self.published = False

    @classmethod
    def create(cls, environ: Mapping[str, str]) -> ScreenResources:
        return cls(
            resolve_screen_http_security(
                environ.get(
                    SCREEN_ALLOWED_ORIGIN_ENV, SCREEN_HTTP_SECURITY_DEFAULT_ORIGIN
                )
            )
        )

    def publish(
        self,
        app: FastAPI,
        *,
        validate_context: Callable[[str, UUID], None],
        inference: InferenceResources,
    ) -> None:
        runtime = inference.runtime
        routing_policy = resolve_routing_policy(runtime.settings, runtime.registry)
        app.state.screen_http_security = self.security
        app.state.screen_perception_service = ScreenPerceptionService(
            vision=VisionInferenceClient(router=runtime.router),
            routing_policy=lambda: routing_policy,
            validate_context=validate_context,
            reference_router=runtime.router,
        )
        self.service = app.state.screen_perception_service
        self.published = True
