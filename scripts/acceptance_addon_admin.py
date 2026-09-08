#!/usr/bin/env python3
"""実Core・Vite・公開MCPの管理画面を、独立した一時data rootで受け入れる。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


BROWSER_CHECKS = {
    "initial": ("registered-list", "initial-available", "toggle-off"),
    "restored": ("restart-restores-off", "on-rechecks-health"),
    "disconnected": ("idle-disconnect-badge", "offline-toggle"),
}


def sanitized_browser_log(stdout, phase):
    """実browserがassert通過後に出す固定eventだけを公開する。"""
    events = []
    for line in stdout.splitlines():
        if not line.startswith("ADDON_ACCEPTANCE "):
            continue
        try:
            event = json.loads(line.removeprefix("ADDON_ACCEPTANCE "))
        except ValueError:
            raise RuntimeError("invalid browser evidence") from None
        if event not in [
            {"check": check, "status": "passed"} for check in BROWSER_CHECKS[phase]
        ]:
            raise RuntimeError("invalid browser evidence")
        events.append(event)
    if [event["check"] for event in events] != list(BROWSER_CHECKS[phase]):
        raise RuntimeError("incomplete browser evidence")
    return "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def process(command, environment, cwd, log):
    with log.open("w") as handle:
        child = subprocess.Popen(
            command,
            env=environment,
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


def ready(url, child):
    until = time.monotonic() + 60
    while time.monotonic() < until:
        if child.poll() is not None:
            raise RuntimeError("acceptance process exited during startup")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError("acceptance startup timeout")


def main():
    if os.environ.get("DS_ENVIRONMENT_ID") == "dogfood":
        raise RuntimeError("dogfood cannot be used for acceptance")
    output = ROOT / "docs/artifacts/addon-admin-184/browser-public.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    log_output = output.with_name("browser-execution.jsonl")
    log_output.unlink(missing_ok=True)
    execution_log = []
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required")
    package = (
        ROOT
        / "infra/testing/mcp-real-servers/node_modules/@modelcontextprotocol/server-everything"
    )
    version = json.loads((package / "package.json").read_text())["version"]
    if version != "2026.8.31":
        raise RuntimeError("published MCP version differs from acceptance contract")
    with (
        tempfile.TemporaryDirectory(prefix="ds-addon-184-") as temporary,
        ExitStack() as stack,
    ):
        work = Path(temporary)
        backend_port, frontend_port, mcp_port = port(), port(), port()
        backend_url = f"http://127.0.0.1:{backend_port}"
        frontend_url = f"http://127.0.0.1:{frontend_port}"
        manifest = {
            "manifest_version": "1.0",
            "connection": {
                "id": "acceptance-mcp",
                "ownership": "external",
                "protocol": "mcp",
                "transport": "streamable_http",
                "endpoint": f"http://127.0.0.1:{mcp_port}/mcp",
                "auth": {"type": "none"},
                "trust": {"annotations": False, "addon_metadata": False},
                "grant": {
                    "scope": "validated_snapshot",
                    "activation": "next_execution_loop",
                },
            },
            "capabilities": {
                "source": "mcp_discovery",
                "tools": [],
                "resources": [],
                "prompts": [],
            },
            "core_policy": {
                "enabled": True,
                "sharing": {"mode": "shared"},
                "resource_binding_required": False,
                "restrictions": [],
                "execution_budget": {
                    "max_calls_per_loop": 6,
                    "max_consecutive_same_tool": 3,
                    "max_identical_call": 2,
                    "normal_max_auto_cycles": 3,
                },
            },
        }
        always_on = deepcopy(manifest)
        always_on["connection"]["id"] = "acceptance-always-on"
        disabled = deepcopy(manifest)
        disabled["connection"]["id"] = "acceptance-disabled"
        disabled["core_policy"]["enabled"] = False
        config = work / "mcp.json"
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "connections": [manifest, always_on, disabled],
                    "display_names": {
                        "acceptance-mcp": "受入用の公開MCP",
                        "acceptance-always-on": "受入用の常時ON接続",
                        "acceptance-disabled": "受入用の未接続OFF",
                    },
                }
            )
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "LANG", "FONTCONFIG_FILE", "FONTCONFIG_PATH"}
        }
        environment.update(
            {
                "PYTHONPATH": str(ROOT / "backend"),
                "PYTHON_DOTENV_DISABLED": "1",
                "DS_ENVIRONMENT_ID": "test",
                "DS_DATA_DIR": str(work / "data"),
                "DS_MCP_CONFIG": str(config),
                "RAG_ENABLED": "false",
            }
        )
        # 管理操作でLLMを呼ばないことを確認するため、Tool Routingは設定しない。
        for target in ("CHAT", "PRIVACY", "MEMORY_EXTRACTION", "MEMORY_CONSOLIDATION"):
            environment.update(
                {
                    f"INFERENCE_TARGET_{target}": "ollama/gemma4:e4b",
                    f"INFERENCE_TARGET_{target}_MAX_INPUT_TOKENS": "12288",
                    f"INFERENCE_TARGET_{target}_MAX_OUTPUT_TOKENS": "1024",
                }
            )
        environment.update(
            {
                "INFERENCE_TARGET_EMBEDDING": "ollama/nomic-embed-text:latest",
                "INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS": "8192",
            }
        )
        mcp = stack.enter_context(
            process(
                [node, str(package / "dist/index.js"), "streamableHttp"],
                {**environment, "PORT": str(mcp_port)},
                work,
                work / "mcp.log",
            )
        )
        command = [
            str(ROOT / "backend/.venv/bin/python"),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
        ]
        backend = stack.enter_context(
            process(command, environment, ROOT, work / "backend.log")
        )
        ready(backend_url + "/addon-admin/connections", backend)
        profile = work / "profile.json"
        profile.write_text(
            json.dumps(
                {
                    "reportSchemaVersion": 1,
                    "effectiveProfile": "addon-admin-acceptance",
                    "readyGate": {
                        "baseUrl": backend_url,
                        "host": "127.0.0.1",
                        "port": backend_port,
                    },
                    "capabilities": [],
                    "dependencies": {
                        name: (
                            {"mode": "real", "source": "external", "baseUrl": url}
                            if url
                            else {"mode": "disabled", "source": None}
                        )
                        for name, url in {
                            "frontend": frontend_url,
                            "backend": backend_url,
                            "ollama": None,
                            "voicevox": None,
                            "whisper": None,
                            "chroma": None,
                        }.items()
                    },
                }
            )
        )
        frontend = stack.enter_context(
            process(
                [
                    node,
                    str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(frontend_port),
                    "--strictPort",
                ],
                {
                    **environment,
                    "DS_PROFILE_REPORT": str(profile),
                    "DS_BACKEND_ORIGIN": backend_url,
                },
                ROOT / "frontend",
                work / "frontend.log",
            )
        )
        ready(frontend_url, frontend)

        def browser(phase):
            result = subprocess.run(
                [
                    node,
                    str(ROOT / "frontend/scripts/acceptance-addon-admin.mjs"),
                    frontend_url,
                    phase,
                    str(work),
                ],
                cwd=ROOT / "frontend",
                env=environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=150,
            )
            if result.returncode:
                (work / "browser-failure.log").write_text(result.stdout)
                raise RuntimeError(f"addon acceptance failed: {phase}")
            execution_log.append(sanitized_browser_log(result.stdout, phase))

        browser("initial")
        backend.terminate()
        backend.wait(timeout=15)
        backend = stack.enter_context(
            process(command, environment, ROOT, work / "backend-restored.log")
        )
        ready(backend_url + "/addon-admin/connections", backend)
        browser("restored")
        mcp.terminate()
        mcp.wait(timeout=10)
        browser("disconnected")
        # public証跡にはendpoint、path、内部process情報、会話本文を含めない。
        report = {
            "schemaVersion": 1,
            "status": "passed",
            "mcp": {
                "package": "@modelcontextprotocol/server-everything",
                "version": version,
            },
            "environment": {
                "runtime": "real-core-vite-browser",
                "toolRoutingConfigured": any(
                    key.startswith("INFERENCE_TARGET_TOOL_ROUTING") for key in environment
                ),
            },
            "checks": [
                json.loads(line)["check"]
                for phase_log in execution_log
                for line in phase_log.splitlines()
            ],
            "testedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    # context終了後にだけ成功証跡を書く。teardown失敗や途中失敗は成功扱いにしない。
    report["teardown"] = "passed"
    log_text = "".join(execution_log)
    log_output.write_text(log_text)
    report["executionLog"] = {
        "path": log_output.name,
        "sha256": hashlib.sha256(log_text.encode()).hexdigest(),
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
