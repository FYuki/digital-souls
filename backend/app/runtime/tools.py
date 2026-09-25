"""Tool runtimeの構築・起動・公開・停止を所有する。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.inference import InferenceTarget
from app.tool_use.runtime import ToolRuntime, ToolSettings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from app.privacy.contracts import PrivacyScanner
    from app.privacy.semantic.classifier import SemanticPrivacyClassifier
    from app.runtime.inference import InferenceResources
    from app.tool_use.service import ToolService


class ToolResources:
    """ToolRuntimeの資源owner。addon公開・起動・tool_service公開を担う。"""

    def __init__(self) -> None:
        self.runtime: ToolRuntime | None = None
        self.routing_service: ToolService | None = None

    def build(
        self,
        settings: ToolSettings,
        *,
        inference: InferenceResources,
        scanner: PrivacyScanner,
        settings_path: Path,
        classifier: SemanticPrivacyClassifier,
        character_exists: Callable[[str], bool],
    ) -> None:
        self.runtime = ToolRuntime(
            settings,
            inference.runtime.router,
            scanner,
            settings_path=settings_path,
            classifier=classifier,
            character_exists=character_exists,
        )

    def publish_addons(self, app: FastAPI) -> None:
        assert self.runtime is not None
        app.state.addon_manager = self.runtime.management
        app.state.event_source = self.runtime.events
        app.state.notifications = self.runtime.notifications
        app.state.action_policy = self.runtime.action_policy

    async def start(self) -> None:
        assert self.runtime is not None
        await self.runtime.start()

    def publish_service(
        self, app: FastAPI, inference: InferenceResources
    ) -> None:
        assert self.runtime is not None
        self.routing_service = (
            self.runtime.service
            if InferenceTarget.TOOL_ROUTING
            in inference.runtime.settings.targets
            else None
        )
        app.state.tool_service = self.routing_service
