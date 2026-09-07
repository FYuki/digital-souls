"""所有する測定用Backendだけで、同梱SDKの実ロードを確認して匿名の証跡を取得する。"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from app.livekit_transport.native_build import validate_record


class NativeSdkSampler:
    def __init__(self, report: Path) -> None:
        self.report = report
        self.container_id: str | None = None

    def sample(self) -> dict[str, Any] | None:
        try:
            report = json.loads(self.report.read_text())
            runtime, profile = report['runtime'], report['effectiveProfile']
            backend = report['services']['backend']
            container = (backend.get('containerIdentity') or {}).get('containerId')
            if (runtime.get('environmentId') != 'test'
                    or runtime.get('dataRoot') != str(self.report.resolve().parents[2])
                    or profile.get('effectiveProfile') not in ('integration-voice', 'integration-voice-fault')
                    or backend.get('owned') is not True
                    or not isinstance(container, str) or not re.fullmatch('[a-f0-9]{64}', container)):
                return None
            if self.container_id is not None and container != self.container_id:
                raise ValueError('native_sdk_container_changed')
            self.container_id = container
            result = subprocess.run(
                ['docker', 'exec', container, 'python', '-m', 'app.livekit_transport.native_build'],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if result.returncode != 0 or len(result.stdout) > 4096:
                return None
            observed = json.loads(result.stdout)
            if observed.get('status') != 'verified':
                return None
            return validate_record(observed['build'])
        except (OSError, KeyError, TypeError, json.JSONDecodeError, subprocess.SubprocessError):
            return None
