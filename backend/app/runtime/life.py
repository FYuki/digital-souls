"""Character Life runtimeの任意起動・公開・停止を所有する。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from app.character_life.cognition import (
    Cognition as LifeCognition,
    Formation as StateFormation,
    Privacy as LifePrivacy,
)
from app.character_life.formation import LifeFormation
from app.character_life.prompt import Context as LifeContext
from app.character_life.runtime import Runtime as LifeRuntime
from app.character_life.runtime import Settings as LifeSettings
from app.character_life.service import Service as LifeService
from app.character_life.store import Store as LifeStore
from app.characters.catalog import CharacterCatalog
from app.inference import InferenceTarget
from app.prompting import PromptMessage

if TYPE_CHECKING:
    from collections.abc import Mapping
    from fastapi import FastAPI
    from app.model_settings import ModelSettings
    from app.runtime.history import HistoryResources
    from app.runtime.inference import InferenceResources
    from app.runtime.tools import ToolResources
    from app.runtime_paths import RuntimePaths
    from app.privacy.semantic.classifier import SemanticPrivacyClassifier
    from pathlib import Path


class LifeResources:
    """Character Lifeの条件判定・構築・起動・公開を担う資源owner。"""

    def __init__(self) -> None:
        self.settings: LifeSettings | None = None
        self.store: LifeStore | None = None
        self.service: LifeService | None = None
        self.runtime: LifeRuntime | None = None
        self.context: LifeContext | None = None
        self.started = False

    def load_settings(self, environ: Mapping[str, str]) -> None:
        self.settings = LifeSettings.load(dict(environ))

    def check_enabled(self, inference: InferenceResources) -> None:
        assert self.settings is not None
        if (
            self.settings.enabled
            and InferenceTarget.CHARACTER_LIFE
            not in inference.runtime.settings.targets
        ):
            raise ValueError(
                "Character Life requires INFERENCE_TARGET_CHARACTER_LIFE"
            )

    async def start_if_enabled(
        self,
        *,
        app: FastAPI,
        paths: RuntimePaths,
        repository_root: Path,
        inference: InferenceResources,
        tools: ToolResources,
        history: HistoryResources,
        privacy_classifier: SemanticPrivacyClassifier,
        model_settings: ModelSettings,
        input_token_counter: Callable[[tuple[PromptMessage, ...]], int],
    ) -> None:
        assert self.settings is not None
        if not self.settings.enabled:
            return
        tool_runtime = tools.runtime
        history_repository = history.repository
        assert tool_runtime is not None and history_repository is not None
        self.store = LifeStore(paths.data_root / "character-life.db")
        self.service = LifeService(
            self.store,
            tool_runtime.gate,
            LifeCognition(inference.runtime.router),
            LifePrivacy(
                tool_runtime.service.sanitizer,
                privacy_classifier,
            ),
            tool_runtime.service.sanitizer,
            foreground_busy=lambda: (
                history_repository.consolidation_activity()[0] > 0
            ),
            bindings=tool_runtime.service.bindings,
        )
        self.service.formation = LifeFormation(
            self.store,
            self.service.reflections,
            StateFormation(inference.runtime.router),
            self.service.privacy,
        )
        character_catalog = CharacterCatalog(repository_root / "characters")
        self.runtime = LifeRuntime(
            self.service,
            paths.data_root,
            self.settings,
            characters=lambda: tuple(
                entry.character_id for entry in character_catalog.scan()
            ),
        )
        await self.runtime.start()
        self.started = True
        app.state.character_life_runtime = self.runtime
        self.context = LifeContext(
            self.store,
            input_token_counter,
            model_settings.chat_context_tokens
            - model_settings.assistant_max_generation_tokens,
            reflections=self.service.reflections,
        )
