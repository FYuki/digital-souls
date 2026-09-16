"""会話・推論を構成せず、同一Backendで通知と連携管理を起動する。"""
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.characters.catalog import CharacterCatalog
from app.memory.memory_policy import resolved_memory_policy
from app.notifications.contracts import load_config
from app.privacy.scanner import create_privacy_scanner
from app.restore_intent import require_no_restore_intent
from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import resolve_runtime_paths
from app.tool_use.runtime import ToolRuntime, ToolSettings


@asynccontextmanager
async def notification_lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 設定検証はデータ領域初期化より先に実行する。通常の設定不備は縮退で隠さない。
    load_config(os.environ.get("DS_NOTIFICATION_CONFIG"))
    settings = ToolSettings.load(os.environ.get("DS_MCP_CONFIG"))
    scanner = create_privacy_scanner(resolved_memory_policy().privacy)
    repository_root = Path(__file__).resolve().parents[3]
    paths = resolve_runtime_paths(os.environ, repository_root)
    initialize_runtime_data_root(paths, repository_root)
    require_no_restore_intent(paths.restore_intent_path)
    catalog = CharacterCatalog(repository_root / "characters")
    runtime = ToolRuntime(
        settings, None, scanner,
        settings_path=paths.data_root / "addon-settings.json",
        character_exists=lambda character: any(
            entry.character_id == character for entry in catalog.scan()
        ),
    )
    app.state.notification_only = True
    app.state.notifications = runtime.notifications
    app.state.addon_manager = runtime.management
    app.state.event_source = runtime.events
    try:
        await runtime.start()
        yield
    finally:
        try:
            await runtime.close()
        finally:
            app.state.notification_only = False
            for name in ("notifications", "addon_manager", "event_source"):
                delattr(app.state, name)


class NotificationAvailabilityMiddleware:
    """未構成の機能へ進まず、HTTP/音声接続へ明示的な利用不可を返す。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and getattr(
            scope["app"].state, "notification_only", False
        ):
            path = scope["path"]
            available = (
                path in {"/", "/health/ready", "/health/inference", "/characters", "/characters/rescan"}
                or path == "/notifications" or path.startswith("/notifications/")
                or path == "/addon-admin" or path.startswith("/addon-admin/")
                or (path.startswith("/characters/") and "/assets/standing/" in path)
            )
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
                return
            if not available:
                await JSONResponse(
                    {"detail": {"code": "conversation_unavailable", "mode": "notifications_only"}},
                    status_code=503, headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)
