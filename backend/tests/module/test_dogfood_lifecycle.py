from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path

import pytest

from orchestrator import classify_preprobe, cleanup_owned_services
from tests.environment_entrypoint_test_support import ROOT_DIR
from tests.environment_test_support import resolved_profile


def _owned_environment_report(
    *, environment_id: str, data_root: Path, frontend_port: int, backend_port: int
) -> dict[str, object]:
    from run_report import create_initial_report, update_service

    profile = deepcopy(resolved_profile(environment_id))
    profile["runtime"] = {
        **profile["runtime"],
        "environmentId": environment_id,
        "dataRoot": str(data_root),
    }
    profile["dependencies"]["frontend"]["baseUrl"] = (
        f"http://127.0.0.1:{frontend_port}"
    )
    profile["dependencies"]["backend"]["baseUrl"] = (
        f"http://127.0.0.1:{backend_port}"
    )
    report = create_initial_report(
        run_id=f"{environment_id}-run",
        started_at="2026-08-07T00:00:00+00:00",
        resolved_profile_path=data_root / "runtime" / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity={
            "pid": 9000,
            "pgid": 9000,
            "sessionId": 9000,
            "startTime": 1,
        },
        runtime=profile["runtime"],
    )
    for offset, service in enumerate(("backend", "frontend"), start=1):
        report = update_service(
            report,
            service,
            state="started",
            owned=True,
            container_identity={
                "containerId": f"{'a' if service == 'backend' else 'b'}" * 64,
                "startedAt": "2026-08-07T00:00:01Z",
            },
        )
    return report


def test_should_classify_ready_exclusive_managed_endpoint_as_conflict() -> None:
    dependency = {"mode": "real", "source": "managed"}

    decision = classify_preprobe(dependency, {"result": "ready", "status": 200})

    assert decision.state == "endpoint_conflict"
    assert decision.failure_category == "startup"


def test_should_keep_external_service_reuse_contract_for_dogfood_dependencies() -> None:
    dependency = {"mode": "real", "source": "external"}

    decision = classify_preprobe(dependency, {"result": "ready", "status": 200})

    assert decision.state == "external"
    assert decision.failure_category is None


def test_should_cleanup_only_owned_services_from_selected_environment_report() -> None:
    dogfood_report = {
        "startSequence": ["backend", "frontend"],
        "services": {
            "backend": {"source": "managed", "state": "started", "owned": True},
            "frontend": {"source": "managed", "state": "started", "owned": True},
            "ollama": {"source": "external", "state": "external", "owned": False},
            "voicevox": {"source": "external", "state": "external", "owned": False},
        },
    }
    stopped: list[str] = []

    results = cleanup_owned_services(
        dogfood_report,
        {
            name: lambda name=name: stopped.append(name) or {"result": "stopped"}
            for name in ("backend", "frontend", "ollama", "voicevox", "dev-backend")
        },
    )

    assert stopped == ["frontend", "backend"]
    assert results == [
        {"service": "frontend", "result": "stopped"},
        {"service": "backend", "result": "stopped"},
    ]


def test_should_keep_integration_owned_services_running_when_dogfood_is_cleaned_up(
    tmp_path: Path,
) -> None:
    from run_report_store import RunReportStore

    integration_root = tmp_path / "integration-data"
    dogfood_root = tmp_path / "dogfood-data"
    reports = {
        "integration": _owned_environment_report(
            environment_id="integration",
            data_root=integration_root,
            frontend_port=5173,
            backend_port=8000,
        ),
        "dogfood": _owned_environment_report(
            environment_id="dogfood",
            data_root=dogfood_root,
            frontend_port=15173,
            backend_port=18000,
        ),
    }
    stores = {
        environment: RunReportStore(
            root / "runtime" / "standalone" / "environment-run.json"
        )
        for environment, root in (
            ("integration", integration_root),
            ("dogfood", dogfood_root),
        )
    }
    observed_endpoints: list[str] = []

    def fake_probe(url: str) -> dict[str, object]:
        observed_endpoints.append(url)
        return {"result": "not_ready"}

    for environment, report in reports.items():
        stores[environment].save(report)
        dependencies = report["effectiveProfile"]["dependencies"]
        frontend_observation = fake_probe(dependencies["frontend"]["baseUrl"])
        assert frontend_observation["result"] == "not_ready"
        assert fake_probe(dependencies["backend"]["baseUrl"])["result"] == "not_ready"

    stopped: list[tuple[str, str]] = []
    dogfood_cleanup = cleanup_owned_services(
        stores["dogfood"].load(),
        {
            service: lambda service=service: stopped.append(("dogfood", service))
            or {"result": "stopped"}
            for service in ("backend", "frontend")
        },
    )

    integration_after_dogfood_cleanup = stores["integration"].load()
    assert observed_endpoints == [
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:15173",
        "http://127.0.0.1:18000",
    ]
    assert stopped == [("dogfood", "frontend"), ("dogfood", "backend")]
    assert dogfood_cleanup == [
        {"service": "frontend", "result": "stopped"},
        {"service": "backend", "result": "stopped"},
    ]
    assert integration_after_dogfood_cleanup["services"]["frontend"]["owned"] is True
    assert integration_after_dogfood_cleanup["services"]["backend"]["owned"] is True


def test_should_forward_resolved_ready_gate_from_profile_to_environment_run(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths,
) -> None:
    import commands.up_command as up_command

    profile = deepcopy(resolved_profile("test-mocked"))
    profile["readyGate"] = {
        "baseUrl": "http://127.0.0.1:14174",
        "host": "127.0.0.1",
        "port": 14174,
    }
    captured_options: dict[str, object] = {}

    class SuccessfulRun:
        def __init__(self, **options):
            captured_options.update(options)
            self.report = options["report"]
            self.store = options["store"]

        def verify(self):
            return None

        def prepare(self):
            return None

        def pre_probe(self):
            return {}

        def start_or_reuse(self, decisions):
            return None

        def wait_until_ready(self):
            from run_report import record_ready
            from run_report_timestamps import next_lifecycle_timestamp

            self.report = self.store.update(
                lambda report: record_ready(
                    report, ready_at=next_lifecycle_timestamp(report)
                )
            )

        def begin_supervision(self):
            return None

        def supervise(self):
            return None

        def cleanup(self):
            return []

    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda env, default, path, legacy, runtime: profile,
    )
    monkeypatch.setattr(up_command, "EnvironmentRun", SuccessfulRun)
    report_path = runtime_paths.runtime_report_dir / "ready-gate" / "environment-run.json"
    arguments = argparse.Namespace(
        run_report=str(report_path),
        profile_report=None,
        default_profile="test-mocked",
    )

    up_command.up_environment(ROOT_DIR, arguments)

    assert captured_options["ready_gate"] == profile["readyGate"]
    assert "ready_gate_url" not in captured_options
