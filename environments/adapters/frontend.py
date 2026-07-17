from __future__ import annotations

import shutil
from pathlib import Path
from typing import Mapping

from adapters.base import (
    Check,
    CommandRunner,
    OperationContext,
    ProcessServiceOperations,
    StartSpecification,
    VerificationResult,
    command_succeeded,
    require_managed_endpoint,
)


class FrontendAdapter(ProcessServiceOperations):
    def __init__(self, root_dir: Path, runner: CommandRunner | None = None) -> None:
        super().__init__(root_dir, "frontend", runner)

    def verify(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> VerificationResult:
        require_managed_endpoint(dependency, service="frontend", port=5173)
        frontend_dir = self.root_dir / "frontend"
        checks = (
            Check(
                "npm",
                "ready" if shutil.which("npm") else "preparation_required",
                "npm command",
                False,
            ),
            Check(
                "frontend-package",
                "ready" if (frontend_dir / "package.json").is_file() else "preparation_required",
                "frontend/package.json",
                False,
            ),
            Check(
                "frontend-dependencies",
                "ready" if (frontend_dir / "node_modules").is_dir() else "preparation_required",
                "frontend/node_modules",
                True,
            ),
        )
        return VerificationResult(checks)

    def prepare(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> None:
        require_managed_endpoint(dependency, service="frontend", port=5173)
        frontend_dir = self.root_dir / "frontend"
        if not (frontend_dir / "node_modules").is_dir():
            result = self.runner.run(("npm", "install", "--prefix", str(frontend_dir)), self.root_dir)
            if not command_succeeded(result):
                raise RuntimeError(f"frontend dependency installation failed: {result.get('stderr', '')}")

    def start_specification(self, dependency: Mapping[str, object]) -> StartSpecification:
        host, port = require_managed_endpoint(dependency, service="frontend", port=5173)
        frontend_dir = self.root_dir / "frontend"
        return StartSpecification(
            command=(
                "npm", "run", "dev", "--prefix", str(frontend_dir), "--",
                "--host", host, "--port", str(port), "--strictPort",
            ),
            cwd=self.root_dir,
        )
