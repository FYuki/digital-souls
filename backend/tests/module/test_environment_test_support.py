from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from tests.environment_entrypoint_test_support import write_executable


def test_should_write_utf8_strict_bash_executable(tmp_path: Path):
    executable = tmp_path / "command"

    write_executable(executable, 'printf "%s\\n" "正常"\n')

    assert executable.read_bytes() == (
        '#!/usr/bin/env bash\nset -euo pipefail\nprintf "%s\\n" "正常"\n'
    ).encode("utf-8")
    assert executable.stat().st_mode & 0o777 == 0o755


def test_should_build_registry_with_only_the_selected_adapter():
    from adapters.base import ServiceOperations
    from environment_constants import DEPENDENCY_NAMES
    from service_registry import ServiceRegistry
    from tests.environment_test_support import single_adapter_registry

    adapter = cast(ServiceOperations, object())

    registry = single_adapter_registry("voicevox", adapter)

    assert isinstance(registry, ServiceRegistry)
    assert set(registry.services) == set(DEPENDENCY_NAMES)
    assert registry.services["voicevox"].adapter is adapter
    assert all(
        registration.adapter is None
        for name, registration in registry.services.items()
        if name != "voicevox"
    )
    assert registry.prepare_order == ("voicevox",)
    assert registry.start_order == ("voicevox",)


@pytest.mark.parametrize(
    ("service", "expected_container"),
    [
        ("frontend", None),
        ("backend", None),
        ("whisper", "backend"),
        ("chroma", "backend"),
    ],
)
def test_should_preserve_registry_containment_for_single_adapter(
    service: str,
    expected_container: str | None,
):
    from adapters.base import ServiceOperations
    from tests.environment_test_support import single_adapter_registry

    adapter = cast(ServiceOperations, object())

    registry = single_adapter_registry(service, adapter)

    assert registry.services[service].contained_by == expected_container
    assert registry.services["whisper"].contained_by == "backend"
    assert registry.services["chroma"].contained_by == "backend"
