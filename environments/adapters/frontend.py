from __future__ import annotations

from pathlib import Path
from typing import Mapping

from adapters.base import (
    CommandRunner,
    OperationContext,
    VerificationResult,
    require_resolved_managed_endpoint,
)
from adapters.compose_service import ComposeManagedServiceOperations
from app.runtime_paths import RuntimePaths


class FrontendAdapter(ComposeManagedServiceOperations):
    def __init__(
        self,
        root_dir: Path,
        runtime_paths: RuntimePaths,
        runner: CommandRunner | None = None,
        *,
        effective_profile: str = "dev",
    ) -> None:
        super().__init__(
            root_dir,
            "frontend",
            runtime_paths,
            runner,
            effective_profile=effective_profile,
        )

    def verify(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> VerificationResult:
        del context
        require_resolved_managed_endpoint(dependency, service="frontend")
        return self.compose_verification()

    def prepare(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> None:
        del context
        require_resolved_managed_endpoint(dependency, service="frontend")
