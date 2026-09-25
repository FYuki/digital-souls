"""推論runtimeの構築・probe・process登録・公開・解放を所有する。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from app.inference.runtime import InferenceRuntime, create_inference_runtime
from app.llm import router as llm_router

if TYPE_CHECKING:
    from fastapi import FastAPI


class InferenceResources:
    """InferenceRuntimeの構築・probe・登録・公開・解放を担う資源owner。"""

    def __init__(self, runtime: InferenceRuntime) -> None:
        self.runtime = runtime
        self.router_registered = False
        self.state_published = False
        self._closed = False

    @classmethod
    def create(cls, environ: Mapping[str, str]) -> InferenceResources:
        return cls(create_inference_runtime(environ))

    def probe(self) -> None:
        try:
            self.runtime.probe_startup()
        except Exception:
            self.close()
            raise

    def publish(self, app: FastAPI) -> None:
        llm_router.register_inference_router(self.runtime.router)
        self.router_registered = True
        app.state.inference_router = self.runtime.router
        app.state.inference_health = self.runtime.health
        self.state_published = True

    def close(self) -> None:
        # 起動途中失敗と正常停止の両方の回収経路から呼ばれるため冪等にする。
        if self._closed:
            return
        self._closed = True
        self.runtime.close()
