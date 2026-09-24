"""ChatServiceの公開と既定resolver登録を所有する。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from app import _chat_runtime

if TYPE_CHECKING:
    from fastapi import FastAPI


class ChatResources:
    """ChatServiceの`app.state`公開と既定resolver登録を担う資源owner。"""

    def __init__(self) -> None:
        self.service: _chat_runtime.ChatService | None = None
        self.resolver: Callable[[], _chat_runtime.ChatService] | None = None
        self.published = False
        self.resolver_registered = False

    def publish(
        self, app: FastAPI, service: _chat_runtime.ChatService
    ) -> None:
        self.service = service
        app.state.chat_service = service
        self.published = True

    def register_resolver(self, app: FastAPI) -> None:
        self.resolver = lambda: cast(
            _chat_runtime.ChatService, app.state.chat_service
        )
        _chat_runtime.register_default_chat_service_resolver(self.resolver)
        self.resolver_registered = True
