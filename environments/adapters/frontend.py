from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

from adapters.base import (
    Check,
    CommandRunner,
    OperationContext,
    ProcessServiceOperations,
    StartSpecification,
    VerificationResult,
    command_succeeded,
    require_resolved_managed_endpoint,
)


def _dogfood_asset_check(name: str, path: Path) -> Check:
    if not path.is_file():
        return Check(
            name,
            "preparation_required",
            f"dogfood Frontend asset is missing: {path}",
            False,
        )
    try:
        with path.open("rb") as asset:
            asset.read(1)
    except OSError:
        return Check(
            name,
            "preparation_required",
            f"EACCES: dogfood Frontend asset is not readable: {path}",
            False,
        )
    return Check(name, "ready", str(path), False)


class FrontendAdapter(ProcessServiceOperations):
    def __init__(
        self,
        root_dir: Path,
        runner: CommandRunner | None = None,
        *,
        effective_profile: str = "dev",
    ) -> None:
        super().__init__(root_dir, "frontend", runner)
        self._effective_profile = effective_profile

    def verify(
        self, dependency: Mapping[str, object], context: OperationContext
    ) -> VerificationResult:
        require_resolved_managed_endpoint(dependency, service="frontend")
        frontend_dir = self.root_dir / "frontend"
        if self._effective_profile == "dogfood":
            required_assets = (
                ("frontend-launcher", frontend_dir / "built-frontend-server.mjs"),
                ("frontend-index", frontend_dir / "dist" / "index.html"),
            )
            dogfood_checks = [
                Check(
                    "node",
                    "ready" if shutil.which("node") else "preparation_required",
                    "node command",
                    False,
                )
            ]
            dogfood_checks.extend(
                _dogfood_asset_check(name, path) for name, path in required_assets
            )
            return VerificationResult(tuple(dogfood_checks))
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
        require_resolved_managed_endpoint(dependency, service="frontend")
        if self._effective_profile == "dogfood":
            return
        frontend_dir = self.root_dir / "frontend"
        if not (frontend_dir / "node_modules").is_dir():
            result = self.runner.run(("npm", "ci", "--prefix", str(frontend_dir)), self.root_dir)
            if not command_succeeded(result):
                raise RuntimeError(f"frontend dependency installation failed: {result.get('stderr', '')}")

    def start_specification(self, dependency: Mapping[str, object]) -> StartSpecification:
        host, port = require_resolved_managed_endpoint(dependency, service="frontend")
        frontend_dir = self.root_dir / "frontend"
        if self._effective_profile == "dogfood":
            return StartSpecification(
                command=(
                    "node",
                    str(frontend_dir / "built-frontend-server.mjs"),
                    "--host",
                    host,
                    "--port",
                    str(port),
                ),
                cwd=self.root_dir,
            )
        return StartSpecification(
            command=(
                "npm", "run", "dev", "--prefix", str(frontend_dir), "--",
                "--host", host, "--port", str(port), "--strictPort",
            ),
            cwd=self.root_dir,
        )
