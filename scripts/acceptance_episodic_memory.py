"""#340実接続受入用の専用Backend/Frontend。共有推論サービスは停止しない。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from uvicorn.config import LOGGING_CONFIG

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.memory.persistence.schema import initialize_persona_memory_schema
from app.runtime_paths import resolve_runtime_paths


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def await_ready(url: str, process: subprocess.Popen, seconds: int = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("owned process exited before readiness")
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("owned process readiness timed out")


def stop_owned(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-root", type=Path, help="正常停止した専用test data rootを保持して再起動")
    args = parser.parse_args()
    if os.environ.get("DS_ENVIRONMENT_ID") == "dogfood":
        raise RuntimeError("dogfood environment is not an acceptance target")
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    if dirty.strip():
        raise RuntimeError("commit the worktree before real acceptance")
    previous_run_id = None
    if args.resume_root is None:
        root = Path(tempfile.mkdtemp(prefix="ds-memory-340-"))
    else:
        root = args.resume_root.resolve(strict=True)
        if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("ds-memory-340-"):
            raise RuntimeError("only this runner's temporary test roots may be resumed")
        previous = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))
        if (previous.get("status") != "stopped" or previous.get("environmentId") != "test"
                or previous.get("dataRoot") != str(root / "data")
                or (root / "data").resolve() != root / "data"):
            raise RuntimeError("resume requires a stopped, isolated test runtime")
        previous_run_id = str(UUID(previous["runId"]))
        (root / f"runtime-manifest-{previous_run_id}.json").write_text(
            json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        # 既に正常停止した専用runnerの停止要求だけを解除する。データは変更しない。
        (root / "stop").unlink(missing_ok=True)
    data = root / "data"
    backend_port, frontend_port = free_port(), free_port()
    backend = f"http://127.0.0.1:{backend_port}"
    frontend = f"http://127.0.0.1:{frontend_port}"
    ollama = "http://127.0.0.1:11434"
    models = httpx.get(ollama + "/api/tags", timeout=10).raise_for_status().json()["models"]
    required = {"gemma4:e4b", "nomic-embed-text:latest"}
    identities = {model["name"]: model["digest"] for model in models if model["name"] in required}
    if identities.keys() != required:
        raise RuntimeError("required local inference models are unavailable")
    environment = {name: os.environ[name] for name in ("PATH", "HOME", "LANG") if name in os.environ}
    environment.update({
        "PYTHONPATH": str(ROOT / "backend"), "PYTHON_DOTENV_DISABLED": "1",
        "DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(data), "OLLAMA_BASE_URL": ollama,
        "RAG_ENABLED": "true", "DS_CHARACTER_LIFE_ENABLED": "false",
        "SCREEN_ALLOWED_ORIGIN": frontend,
        "LLM_CONTEXT_TOKEN_LIMIT": "36864",
        "MEMORY_FORMATION_LLM_TIMEOUT_SECONDS": "180",
        "MEMORY_FORMATION_TOTAL_TIMEOUT_SECONDS": "400",
    })
    token_limits = {}
    for target, output in (("CHAT", 1024), ("PRIVACY", 512),
                           ("MEMORY_EXTRACTION", 4096), ("MEMORY_CONSOLIDATION", 512)):
        token_limits[target] = {"input": 36864 - output, "output": output}
        environment.update({
            f"INFERENCE_TARGET_{target}": "ollama/gemma4:e4b",
            f"INFERENCE_TARGET_{target}_MAX_INPUT_TOKENS": str(36864 - output),
            f"INFERENCE_TARGET_{target}_MAX_OUTPUT_TOKENS": str(output),
            f"INFERENCE_TARGET_{target}_TIMEOUT_SECONDS": "180",
            f"INFERENCE_TARGET_{target}_OPTIONS_JSON": json.dumps({"temperature": 0, "think": False}),
        })
    environment.update({
        "INFERENCE_TARGET_EMBEDDING": "ollama/nomic-embed-text:latest",
        "INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS": "8192",
        "INFERENCE_TARGET_EMBEDDING_TIMEOUT_SECONDS": "120",
    })
    # 常用のdata rootをコピーせず、新規のtest identityと空の正本を用意する。
    paths = resolve_runtime_paths(environment, ROOT)
    initialize_persona_memory_schema(paths, ROOT)
    profile = root / "profile.json"
    profile.write_text(json.dumps({
        "reportSchemaVersion": 1, "effectiveProfile": "episodic-memory-acceptance",
        "readyGate": {"baseUrl": backend, "host": "127.0.0.1", "port": backend_port},
        "capabilities": ["text-chat-real"],
        "dependencies": {
            "frontend": {"mode": "real", "source": "external", "baseUrl": frontend},
            "backend": {"mode": "real", "source": "external", "baseUrl": backend},
            "ollama": {"mode": "real", "source": "external", "baseUrl": ollama},
            "chroma": {"mode": "real", "source": "in_process"},
            "voicevox": {"mode": "disabled", "source": None},
            "whisper": {"mode": "disabled", "source": None},
        },
    }), encoding="utf-8")
    logging_config = deepcopy(LOGGING_CONFIG)
    # このloggerのINFOは採用した記憶のID・版・日時精度だけ。本文やpromptは記録しない。
    logging_config["loggers"]["app._chat_runtime"] = {
        "handlers": ["default"], "level": "INFO", "propagate": False,
    }
    log_config_path = root / "logging.json"
    log_config_path.write_text(json.dumps(logging_config), encoding="utf-8")
    manifest = {
        "previousRunId": previous_run_id,
        "schemaVersion": 1, "runId": str(uuid4()), "status": "starting",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "environmentId": "test", "dataRoot": str(data), "models": identities,
        "tokenLimits": token_limits, "backend": backend, "frontend": frontend,
        "externalOllama": ollama, "ownedProcesses": {},
        "acceptanceStatus": "unverified",
    }
    manifest_path = root / "runtime-manifest.json"
    def record_state() -> None:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    record_state()
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    processes = []
    try:
        with (root / "backend.log").open("ab") as backend_log, (root / "frontend.log").open("ab") as frontend_log:
            api = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                 "--port", str(backend_port), "--no-access-log", "--log-level", "warning",
                 "--log-config", str(log_config_path)],
                cwd=ROOT, env=environment, stdout=backend_log, stderr=subprocess.STDOUT,
            )
            processes.append(api)
            manifest["ownedProcesses"]["backend"] = api.pid
            record_state()
            await_ready(backend + "/health/ready", api)
            ui = subprocess.Popen(
                ["node", str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
                 "--host", "127.0.0.1", "--port", str(frontend_port), "--strictPort"],
                cwd=ROOT / "frontend",
                env={**environment, "DS_PROFILE_REPORT": str(profile), "DS_BACKEND_ORIGIN": backend},
                stdout=frontend_log, stderr=subprocess.STDOUT,
            )
            processes.append(ui)
            manifest["ownedProcesses"]["frontend"] = ui.pid
            await_ready(frontend + "/", ui)
            manifest["status"] = "ready"
            record_state()
            print(json.dumps({"phase": "ready", "runRoot": str(root), "backend": backend, "frontend": frontend}), flush=True)
            while not stopped.wait(0.5) and not (root / "stop").exists():
                if any(process.poll() is not None for process in processes):
                    raise RuntimeError("owned acceptance process exited")
            manifest["status"] = "stopped"
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        manifest["status"] = "failed"
        manifest["errorClass"] = type(error).__name__
        print(json.dumps({"phase": "failed", "runRoot": str(root), "errorClass": type(error).__name__}), flush=True)
        return 1
    finally:
        for process in reversed(processes):
            stop_owned(process)
        record_state()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
