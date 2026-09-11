"""#182のブラウザ受入用runtime。既存のdev/dogfoodプロセスを操作しない。"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import wave
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx
from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import resolve_runtime_paths
from dotenv import dotenv_values
from tests.external_mcp_test_support import manifest
from tests.integration.test_external_mcp_real_servers_integration import (
    everything_http,
    free_port,
    stop_process,
)
from uvicorn.config import LOGGING_CONFIG


def public_evidence(text: str, temporary_root: Path) -> str:
    """公開用の写しだけからローカルpathと動的な接続先を除く。"""
    text = text.replace(str(temporary_root), "<test-root>")
    text = text.replace(str(ROOT), ".")
    return re.sub(
        r"(?:https?|wss?)://(?:localhost|127\.0\.0\.1):\d+", "<test-service>", text
    )


def public_browser_report(value, temporary_root: Path):
    """Playwrightがbase64にした合成会話attachmentも公開用pathへ置換する。"""
    if isinstance(value, list):
        return [public_browser_report(item, temporary_root) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: public_browser_report(item, temporary_root) for key, item in value.items()
    }
    if (
        result.get("contentType") in {"application/json", "text/plain"}
        and "body" in result
    ):
        decoded = base64.b64decode(result["body"], validate=True).decode("utf-8")
        result["body"] = base64.b64encode(
            public_evidence(decoded, temporary_root).encode()
        ).decode()
    return result


@contextmanager
def owned_process(command, environment, directory, logfile):
    with logfile.open("wb") as output:
        process = subprocess.Popen(
            command,
            cwd=directory,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            yield process
        finally:
            stop_process(process)


def ready(url, process, *, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("テスト用processが起動時に終了しました")
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("テスト用processのreadiness期限が切れました")


def speech(base, text, path, *, silence=90):
    query = httpx.post(
        base + "/audio_query", params={"text": text, "speaker": 3}, timeout=30
    ).json()
    result = httpx.post(
        base + "/synthesis", params={"speaker": 3}, json=query, timeout=30
    )
    result.raise_for_status()
    with (
        wave.open(io.BytesIO(result.content)) as source,
        wave.open(str(path), "wb") as output,
    ):
        output.setparams(source.getparams())
        output.writeframes(source.readframes(source.getnframes()))
        output.writeframes(
            bytes(
                source.getframerate()
                * source.getnchannels()
                * source.getsampwidth()
                * silence
            )
        )


def voicevox_runtime_identity(configured):
    """指定された稼働中コンテナの不変image IDだけを読み取り、環境変数は取得しない。"""
    container = configured.get("ACCEPTANCE_VOICEVOX_CONTAINER")
    if not container:
        return None
    distribution = configured.get("ACCEPTANCE_VOICEVOX_WSL_DISTRIBUTION")
    command = ["docker"]
    if distribution:
        launcher = shutil.which("wsl.exe") or "/mnt/c/Windows/System32/wsl.exe"
        command = [launcher, "--distribution", distribution, "--cd", "/tmp", "--exec", "docker"]
    result = subprocess.run(
        [*command, "inspect", "--type=container", "--format", "{{.State.Running}} {{.Image}}", container],
        capture_output=True, text=True, timeout=15, check=True,
    )
    parts = result.stdout.strip().split()
    if len(parts) != 2 or parts[0] != "true" or not re.fullmatch(r"sha256:[0-9a-f]{64}", parts[1]):
        raise RuntimeError("VOICEVOXの稼働中コンテナのimage IDを確認できません")
    return {"imageId": parts[1], "source": "running-container"}


def main():
    contract = "--contract-mcp" in sys.argv
    action = "--addon-action" in sys.argv
    if contract and action:
        raise ValueError("会話契約fixtureと独立MCPの承認受入は別runで実行してください")
    arguments = [
        a for a in sys.argv[1:] if a not in {"--contract-mcp", "--addon-action"}
    ]
    if os.environ.get("DS_ENVIRONMENT_ID") == "dogfood":
        raise RuntimeError("dogfoodから受入テストを起動できません")
    artifacts = (
        ROOT
        / "frontend/test-results"
        / (
            "addon-action-runtime"
            if action
            else "tool-use-contract-runtime"
            if contract
            else "tool-use-runtime"
        )
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    # 起動・readiness失敗でも前回の成功証跡を今回の結果として残さない。
    (artifacts / "resolved-profile.json").unlink(missing_ok=True)
    (artifacts / "runtime-manifest.json").unlink(missing_ok=True)
    (artifacts / "browser-public.json").unlink(missing_ok=True)
    node = shutil.which("node")
    docker = shutil.which("docker")
    assert node and docker
    packages = (
        ROOT / "infra/testing/mcp-real-servers/node_modules/@modelcontextprotocol"
    )
    servers = {
        "node": node,
        "everything": str(packages / "server-everything/dist/index.js"),
    }
    if not contract:
        assert Path(servers["everything"]).is_file()
        for name in ["filesystem"] if action else ["filesystem", "everything"]:
            package = json.loads((packages / f"server-{name}/package.json").read_text())
            assert package["version"] == "2026.8.31"
    # 認証値はprocess環境にだけ渡し、reportへ書かない。
    configured = {
        **dotenv_values(
            os.environ.get("ACCEPTANCE_INFERENCE_ENV", str(ROOT / "backend/.env"))
        ),
        **os.environ,
    }
    environment = {
        k: str(v)
        for k, v in configured.items()
        if v is not None
        and (
            k in {"PATH", "HOME", "LANG"}
            or k.startswith(("INFERENCE_", "OLLAMA_", "VOICEVOX_", "WHISPER_"))
        )
    }
    # optionalなクラウドTargetはこのローカル受入では起動しない。
    environment = {
        k: v
        for k, v in environment.items()
        if not k.startswith("INFERENCE_TARGET_HEAVY_REASONING")
    }
    for token, output in (
        ("CHAT", 1024),
        ("PRIVACY", 512),
        ("MEMORY_EXTRACTION", 512),
        ("MEMORY_CONSOLIDATION", 512),
        ("TOOL_ROUTING", 1024),
    ):
        environment.update(
            {
                f"INFERENCE_TARGET_{token}": "ollama/gemma4:e4b",
                # 同じOllamaモデルの用途切替でcontext枠を変えず、既存の合計8192に揃える。
                f"INFERENCE_TARGET_{token}_MAX_INPUT_TOKENS": str(8192 - output),
                f"INFERENCE_TARGET_{token}_MAX_OUTPUT_TOKENS": str(output),
            }
        )
    environment.update(
        {
            "INFERENCE_TARGET_EMBEDDING": "ollama/nomic-embed-text:latest",
            "INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS": "8192",
            "INFERENCE_TARGET_TOOL_ROUTING_TIMEOUT_SECONDS": "90",
            "INFERENCE_TARGET_TOOL_ROUTING_OPTIONS_JSON": '{"temperature":0}',
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONPATH": str(ROOT / "backend"),
            "DS_ENVIRONMENT_ID": "test",
            "RAG_ENABLED": "false",
        }
    )
    with (
        tempfile.TemporaryDirectory(prefix="ds-tool-182-") as temporary,
        ExitStack() as stack,
    ):
        root = Path(temporary)
        data = root / "data"
        environment["DS_DATA_DIR"] = str(data)
        initialize_runtime_data_root(resolve_runtime_paths(environment, ROOT), ROOT)
        runtime = data / "runtime" / "tool-use"
        runtime.mkdir(parents=True, exist_ok=True)
        files = root / "files"
        files.mkdir()
        sample = files / "sample.txt"
        sample.write_text("展示テーマは青い折り紙です。", encoding="utf-8")
        endpoint = None
        if not contract and not action:
            endpoint, _http = stack.enter_context(everything_http(servers, runtime))
        backend_port, frontend_port = free_port(), free_port()
        livekit_port, tcp_port, udp_port = free_port(), free_port(), free_port()
        key, secret = "test-182", secrets.token_hex(32)
        livekit_config = root / "livekit.json"
        livekit_config.write_text(
            json.dumps(
                {
                    "port": livekit_port,
                    "bind_addresses": ["127.0.0.1"],
                    "rtc": {
                        "tcp_port": tcp_port,
                        "udp_port": udp_port,
                        "use_external_ip": False,
                    },
                    "keys": {key: secret},
                }
            )
        )
        livekit_config.chmod(0o600)
        container = "ds-tool-182-" + secrets.token_hex(6)
        lk = stack.enter_context(
            owned_process(
                [
                    docker,
                    "run",
                    "--rm",
                    "--name",
                    container,
                    "--network",
                    "host",
                    "-v",
                    f"{livekit_config}:/etc/livekit.yaml:ro",
                    "livekit/livekit-server:v1.9.7",
                    "--config",
                    "/etc/livekit.yaml",
                ],
                os.environ,
                ROOT,
                runtime / "livekit.log",
            )
        )
        stack.callback(
            lambda: subprocess.run(
                [docker, "stop", "--time", "5", container],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        ready(f"http://127.0.0.1:{livekit_port}/", lk)
        filesystem = manifest(
            connection_id="acceptance-filesystem", trusted=action, binding=action
        )
        if action:
            filesystem["core_policy"]["operation_allowlist"] = [
                "read_text_file",
                "write_file",
            ]
        filesystem["connection"]["stdio"] = {
            "command": node,
            "args": [str(packages / "server-filesystem/dist/index.js"), str(files)],
        }
        config = root / "mcp.json"
        connections = [filesystem]
        if not action:
            connections.append(
                manifest(
                    connection_id="acceptance-http",
                    transport="streamable_http",
                    endpoint=endpoint,
                )
            )
        signals = root / "signals"
        signals.mkdir()
        if contract:
            fixture = manifest(connection_id="controlled-conversation")
            fixture["connection"]["stdio"] = {
                "command": str(ROOT / "backend/.venv/bin/python"),
                "args": [
                    str(
                        ROOT
                        / "backend/tests/fixtures/external_mcp/tool_conversation_server.py"
                    ),
                    "--signals",
                    str(signals),
                ],
            }
            connections = [fixture]
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "connections": connections,
                    "bindings": [
                        {
                            "id": "acceptance-file",
                            "connection_id": "acceptance-filesystem",
                            "character_id": "miori",
                            "label": "検証用ファイル",
                            "operations": ["read_text_file", "write_file"],
                            "arguments": {"path": str(sample)},
                        }
                    ]
                    if action
                    else [],
                }
            )
        )
        environment.update(
            {
                "DS_DATA_DIR": str(data),
                "DS_MCP_CONFIG": str(config),
                "LIVEKIT_URL": f"ws://127.0.0.1:{livekit_port}",
                "LIVEKIT_API_KEY": key,
                "LIVEKIT_API_SECRET": secret,
            }
        )
        backend_url = f"http://127.0.0.1:{backend_port}"
        frontend_url = f"http://127.0.0.1:{frontend_port}"
        environment["SCREEN_ALLOWED_ORIGIN"] = frontend_url
        logging_config = deepcopy(LOGGING_CONFIG)
        logging_config["loggers"]["app.tool_use"] = {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,
        }
        if action:
            # 既存observerの固定metadataだけを記録する。本文・引数・認証情報は含まない。
            logging_config["loggers"]["app.inference.runtime"] = {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            }
        log_config_path = root / "logging.json"
        log_config_path.write_text(json.dumps(logging_config))
        backend = stack.enter_context(
            owned_process(
                [
                    str(ROOT / "backend/.venv/bin/python"),
                    *(
                        [
                            str(ROOT / "scripts/acceptance_backend.py"),
                        ]
                        if action
                        else ["-m", "uvicorn"]
                    ),
                    "app.main:app",
                    "--log-config",
                    str(log_config_path),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(backend_port),
                ],
                environment,
                ROOT,
                runtime / "backend.log",
            )
        )
        ready(backend_url + "/health/ready", backend)
        profile = runtime / "resolved-profile.json"
        service_urls = {
            "backend": backend_url,
            "frontend": frontend_url,
            "ollama": environment.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            "voicevox": environment.get("VOICEVOX_BASE_URL", "http://127.0.0.1:50021"),
            "whisper": environment.get("WHISPER_BASE_URL", "http://127.0.0.1:50022"),
        }
        profile.write_text(
            json.dumps(
                {
                    "reportSchemaVersion": 1,
                    "effectiveProfile": "tool-use-acceptance",
                    "readyGate": {
                        "baseUrl": backend_url,
                        "host": "127.0.0.1",
                        "port": backend_port,
                    },
                    "capabilities": ["voice-chat-real"],
                    "dependencies": {
                        name: (
                            {"mode": "disabled", "source": None}
                            if name == "chroma"
                            else {
                                "mode": "real",
                                "source": "external",
                                "baseUrl": service_urls[name],
                            }
                        )
                        for name in (
                            "frontend",
                            "backend",
                            "ollama",
                            "voicevox",
                            "whisper",
                            "chroma",
                        )
                    },
                }
            )
        )
        frontend_env = {
            **os.environ,
            "DS_PROFILE_REPORT": str(profile),
            "DS_BACKEND_ORIGIN": backend_url,
        }
        frontend = stack.enter_context(
            owned_process(
                [
                    node,
                    str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(frontend_port),
                    "--strictPort",
                ],
                frontend_env,
                ROOT / "frontend",
                runtime / "frontend.log",
            )
        )
        ready(frontend_url, frontend)

        run_manifest = {
            "schemaVersion": 1,
            "runId": str(uuid4()),
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "environmentId": "test",
            "mcpImplementation": "controlled-fixture"
            if contract
            else "published-filesystem"
            if action
            else "published-filesystem-and-everything",
            "mcpVersion": None if contract else "2026.8.31",
            "livekitVersion": "1.9.7",
            "microphone": "scheduled-voicevox-wav-mediastream"
            if contract or action
            else "chromium-voicevox-wav",
            "routingModel": environment["INFERENCE_TARGET_TOOL_ROUTING"],
            "inferenceTokenLimits": {
                name.lower(): {
                    "input": int(
                        environment[f"INFERENCE_TARGET_{name}_MAX_INPUT_TOKENS"]
                    ),
                    "output": int(
                        environment[f"INFERENCE_TARGET_{name}_MAX_OUTPUT_TOKENS"]
                    ),
                }
                for name in (
                    "CHAT",
                    "PRIVACY",
                    "MEMORY_EXTRACTION",
                    "MEMORY_CONSOLIDATION",
                    "TOOL_ROUTING",
                )
            },
            "implementationCommit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "trackedChangesAtStart": bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain", "--untracked-files=no"],
                    cwd=ROOT,
                    text=True,
                ).strip()
            ),
            "dataRoot": str(data),
            "ownedProcesses": {"backend": backend.pid, "frontend": frontend.pid},
            "ownedLiveKitContainer": container,
            "externalServices": {
                k: v
                for k, v in service_urls.items()
                if k not in {"backend", "frontend"}
            },
            "testStatus": "running",
        }
        if action:
            versions = {}
            for name, path in (
                ("whisper", "/version"),
                ("voicevox", "/version"),
                ("ollama", "/api/version"),
            ):
                response = httpx.get(service_urls[name] + path, timeout=10)
                response.raise_for_status()
                versions[name] = response.json()
            voicevox_identity = voicevox_runtime_identity(configured)
            if voicevox_identity is not None:
                versions["voicevoxRuntime"] = voicevox_identity
            run_manifest["serviceVersions"] = versions
        (runtime / "runtime-manifest.json").write_text(
            json.dumps(run_manifest, ensure_ascii=False, indent=2)
        )
        # resolved profileを公開証跡へ複製しない。接続先はテスト用data rootだけに置く。
        voicevox = environment.get("VOICEVOX_BASE_URL", "http://127.0.0.1:50021")
        speech(
            voicevox,
            "先ほどのファイルを読んでください。展示のテーマを教えてください。",
            root / "filesystem.wav",
        )
        speech(
            voicevox,
            "外部資料のスタートアップ、ドット、エムディーを読んで、サーバーの起動方法を短く教えてください。",
            root / "resource.wav",
        )
        test_env = {
            **os.environ,
            "TOOL_USE_TEST_FRONTEND_URL": frontend_url,
            "TOOL_USE_TEST_SAMPLE": str(sample),
            "TOOL_USE_TEST_AUDIO_DIR": str(root),
            "TOOL_USE_TEST_SIGNALS": str(signals),
            "TOOL_USE_TEST_RESULTS_DIR": str(runtime / "browser"),
        }
        if action:
            for name, text in {
                "action-once": "先ほど読み書きした検証用ファイルと同じパスです。そのファイルの内容を、青い星、という三文字だけに置き換えてください。",
                "spoken-approval": "一度承認します。実行してください。",
                "action-reject": "先ほど読み書きした検証用ファイルと同じパスです。そのファイルの内容を、緑の月、という三文字だけに置き換えてください。",
                "action-always": "先ほど読み書きした検証用ファイルと同じパスです。そのファイルの内容を、金の花、という三文字だけに置き換えてください。",
            }.items():
                speech(voicevox, text, root / f"{name}.wav", silence=0)
        if contract:
            speech(
                voicevox,
                "外部ツールを使って、展示の案内を準備してください。",
                root / "question.wav",
                silence=0,
            )
            speech(voicevox, "展示の色は赤色です。", root / "answer.wav", silence=0)
            speech(
                voicevox,
                "時間のかかる確認という名前の外部ツールを実行してください。このツールは引数なしで実行できます。",
                root / "slow.wav",
                silence=0,
            )
            speech(
                voicevox,
                "確認はやめて、普通に挨拶してください。こんにちは。",
                root / "greeting.wav",
                silence=0,
            )
        with (runtime / "playwright.log").open("w") as output:
            result = subprocess.run(
                [
                    str(ROOT / "frontend/node_modules/.bin/playwright"),
                    "test",
                    "--config",
                    "playwright.addon-action.config.ts"
                    if action
                    else "playwright.tool-use-contract.config.ts"
                    if contract
                    else "playwright.tool-use.config.ts",
                    *arguments,
                ],
                check=False,
                cwd=ROOT / "frontend",
                env=test_env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        print(
            public_evidence((runtime / "playwright.log").read_text(), root),
            end="",
            flush=True,
        )
        run_manifest["testStatus"] = "passed" if result.returncode == 0 else "failed"
        run_manifest["completedAt"] = datetime.now(timezone.utc).isoformat()
        (runtime / "runtime-manifest.json").write_text(
            json.dumps(run_manifest, ensure_ascii=False, indent=2)
        )
        shared = {
            k: v
            for k, v in run_manifest.items()
            if k
            not in {
                "dataRoot",
                "ownedProcesses",
                "ownedLiveKitContainer",
                "externalServices",
            }
        }
        # 使用modelはIssueの実接続受入条件として記録する。credentialは含まない。
        (artifacts / "runtime-manifest.json").write_text(
            json.dumps(shared, ensure_ascii=False, indent=2)
        )
        report = runtime / "browser" / "results.json"
        if report.is_file():
            (artifacts / "browser-public.json").write_text(
                public_evidence(
                    json.dumps(
                        public_browser_report(json.loads(report.read_text()), root),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    root,
                )
            )
        if action:
            # 停滞診断はframe位置だけを残す。ローカル変数や例外本文は含めない。
            stacks = [
                line
                for line in (runtime / "backend.log").read_text().splitlines()
                if line.startswith(
                    (
                        "Thread ",
                        "Current thread ",
                        "  File ",
                        "Timeout (",
                        "  <no Python frame>",
                        "Acceptance task:",
                        "Acceptance frame:",
                    )
                )
            ]
            (artifacts / "thread-stacks.txt").write_text(
                public_evidence("\n".join(stacks) + "\n", root)
            )
            stages = [
                line
                for line in (runtime / "backend.log").read_text().splitlines()
                if "Tool decision:" in line
                or "Tool result:" in line
                or "Tool routing stopped:" in line
                or "Tool confirmation continuation:" in line
                or '"event":"inference_request"' in line
                or '"event": "inference_request"' in line
            ]
            (artifacts / "stage-events.txt").write_text(
                public_evidence("\n".join(stages) + "\n", root)
            )
            with sqlite3.connect(
                f"file:{data / 'addon-actions/actions.sqlite3'}?mode=ro", uri=True
            ) as db:
                state = {
                    "confirmations": db.execute(
                        "SELECT operation_group, scene, choice, waiting FROM action_confirmations"
                    ).fetchall(),
                    "executions": db.execute(
                        "SELECT operation, outcome FROM action_executions"
                    ).fetchall(),
                }
            (artifacts / "action-state.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2)
            )
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
