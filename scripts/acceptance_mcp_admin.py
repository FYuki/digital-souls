#!/usr/bin/env python3
"""実Core・Vite・ブラウザと制御MCPのHTTP/stdioを、独立data rootで受け入れる。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from acceptance_addon_admin import port, ready

ROOT = Path(__file__).resolve().parents[1]
CHECKS = {
    "initial": [
        "http-register-off-enable",
        "display-name-edit-keeps-confirmed",
        "bearer-duplicate-isolation-and-rotation",
        "stdio-register-discovery",
        "failed-save-and-enable-remain-off",
        "zero-tools-and-responsive-list",
    ],
    "restored": ["restart-restores-settings-intent-history-and-credential"],
    "disconnected": [
        "disconnect-and-settings-change-keep-on-intent",
        "delete-all-connections-and-owned-credentials",
    ],
}


def stop_process(child):
    # 回収済みPIDへ再送信しない。groupへ通知してからleaderをwaitする。
    if child.returncode is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


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
            stop_process(child)


def listen(port_number, child):
    until = time.monotonic() + 30
    while time.monotonic() < until:
        if child.poll() is not None:
            raise RuntimeError("fixture startup failed")
        try:
            with socket.create_connection(("127.0.0.1", port_number), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("fixture startup timeout")


def main():
    if os.environ.get("DS_ENVIRONMENT_ID") == "dogfood":
        raise RuntimeError("dogfood cannot be used for acceptance")
    output = ROOT / "docs/artifacts/mcp-admin-242/browser-public.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    event_path = output.with_name("browser-execution.jsonl")
    event_path.unlink(missing_ok=True)
    diagnostic = Path(tempfile.mkdtemp(prefix="ds-mcp-admin-diagnostics-"))
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is required")
    python = str(ROOT / "backend/.venv/bin/python")
    server = str(ROOT / "backend/tests/fixtures/external_mcp/server.py")
    events = []
    with (
        tempfile.TemporaryDirectory(prefix="ds-mcp-admin-242-") as temporary,
        ExitStack() as stack,
    ):
        work = Path(temporary)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "LANG", "FONTCONFIG_FILE", "FONTCONFIG_PATH"}
        }
        environment.update(
            PYTHONPATH=str(ROOT / "backend"),
            PYTHON_DOTENV_DISABLED="1",
            DS_ENVIRONMENT_ID="test",
            DS_DATA_DIR=str(work / "data"),
            RAG_ENABLED="false",
        )
        for target in ("CHAT", "PRIVACY", "MEMORY_EXTRACTION", "MEMORY_CONSOLIDATION"):
            environment.update(
                {
                    f"INFERENCE_TARGET_{target}": "ollama/gemma4:e4b",
                    f"INFERENCE_TARGET_{target}_MAX_INPUT_TOKENS": "12288",
                    f"INFERENCE_TARGET_{target}_MAX_OUTPUT_TOKENS": "1024",
                }
            )
        environment.update(
            INFERENCE_TARGET_EMBEDDING="ollama/nomic-embed-text:latest",
            INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS="8192",
        )
        ollama = shutil.which("ollama")
        models = Path(
            os.environ.get(
                "MCP_ACCEPTANCE_OLLAMA_MODELS", "/usr/share/ollama/.ollama/models"
            )
        )
        if (
            not ollama
            or not (models / "manifests/registry.ollama.ai/library/gemma4/e4b").exists()
        ):
            raise RuntimeError(
                "Ollama and existing gemma4:e4b/nomic-embed-text models are required"
            )
        ollama_port = port()
        ollama_url = f"http://127.0.0.1:{ollama_port}"
        ollama_process = stack.enter_context(
            process(
                [ollama, "serve"],
                {
                    **environment,
                    "OLLAMA_HOST": f"127.0.0.1:{ollama_port}",
                    "OLLAMA_MODELS": str(models),
                },
                ROOT,
                diagnostic / "ollama.log",
            )
        )
        ready(ollama_url + "/api/tags", ollama_process)
        environment["OLLAMA_BASE_URL"] = ollama_url
        endpoints = {}
        fixtures = {}
        for name, arguments in (
            ("http", []),
            ("bearer", ["--auth", "bearer"]),
            ("empty", ["--empty"]),
        ):
            number = port()
            child = stack.enter_context(
                process(
                    [python, server, "--port", str(number), *arguments],
                    environment,
                    ROOT,
                    diagnostic / f"{name}.log",
                )
            )
            listen(number, child)
            endpoints[name] = f"http://127.0.0.1:{number}/mcp"
            fixtures[name] = child
        backend_port, frontend_port = port(), port()
        backend_url = f"http://127.0.0.1:{backend_port}"
        frontend_url = f"http://127.0.0.1:{frontend_port}"
        command = [
            python,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
        ]
        backend = stack.enter_context(
            process(command, environment, ROOT, diagnostic / "backend.log")
        )
        ready(backend_url + "/addon-admin/connections", backend)
        profile = work / "profile.json"
        profile.write_text(
            json.dumps(
                {
                    "reportSchemaVersion": 1,
                    "effectiveProfile": "mcp-admin-acceptance",
                    "readyGate": {
                        "baseUrl": backend_url,
                        "host": "127.0.0.1",
                        "port": backend_port,
                    },
                    "capabilities": [],
                    "dependencies": {
                        name: {"mode": "real", "source": "external", "baseUrl": url}
                        if url
                        else {"mode": "disabled", "source": None}
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
                diagnostic / "frontend.log",
            )
        )
        ready(frontend_url, frontend)
        config = work / "browser.json"
        config.write_text(
            json.dumps(
                {
                    **endpoints,
                    "frontend": frontend_url,
                    "python": python,
                    "server": server,
                    "unreachable": f"http://127.0.0.1:{port()}/mcp",
                    "screenshot": str(diagnostic / "management.png"),
                }
            )
        )

        def browser(phase):
            result = subprocess.run(
                [
                    node,
                    str(ROOT / "frontend/scripts/acceptance-mcp-admin.mjs"),
                    str(config),
                    phase,
                ],
                env=environment,
                cwd=ROOT / "frontend",
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
            (diagnostic / f"browser-{phase}.log").write_text(
                result.stdout + result.stderr
            )
            if result.returncode:
                raise RuntimeError(
                    f"browser acceptance failed: {phase}; diagnostic={diagnostic}"
                )
            current = [
                json.loads(line.removeprefix("MCP_ADMIN_ACCEPTANCE "))
                for line in result.stdout.splitlines()
                if line.startswith("MCP_ADMIN_ACCEPTANCE ")
            ]
            if current != [
                {"check": check, "status": "passed"} for check in CHECKS[phase]
            ]:
                raise RuntimeError("incomplete browser evidence")
            events.extend(current)
            print(f"{phase}: {len(current)} checks passed", flush=True)

        browser("initial")
        stop_process(backend)
        backend = stack.enter_context(
            process(command, environment, ROOT, diagnostic / "backend-restored.log")
        )
        ready(backend_url + "/addon-admin/connections", backend)
        browser("restored")
        stop_process(fixtures["http"])
        browser("disconnected")
        with sqlite3.connect(work / "data/mcp-admin/connections.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] == 0
        events.append(
            {"check": "database-connection-and-credential-deletion", "status": "passed"}
        )
    # 全processの停止・log fileのclose後に終了時の出力まで検査する。
    for log in diagnostic.glob("*.log"):
        text = log.read_text()
        for private in (
            "synthetic-test-token",
            "synthetic-wrong-token",
            "private-auth-error",
        ):
            if private in text:
                raise RuntimeError("private value in acceptance log")
    events.append(
        {"check": "no-credential-in-process-or-browser-logs", "status": "passed"}
    )
    event_text = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    event_path.write_text(event_text)
    report = {
        "schemaVersion": 1,
        "status": "passed",
        "runtime": "real-core-vite-browser",
        "mcp": "controlled-external-process-http-none-bearer-stdio",
        "thirdPartyServiceAcceptance": False,
        "toolRoutingConfigured": False,
        "inference": "real-ollama-startup-model-probes-only",
        "checks": [event["check"] for event in events],
        "teardown": "passed",
        "testedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "executionLog": {
            "path": event_path.name,
            "sha256": hashlib.sha256(event_text.encode()).hexdigest(),
        },
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    print(f"Local diagnostic: {diagnostic}")


if __name__ == "__main__":
    main()
