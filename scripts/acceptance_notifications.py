#!/usr/bin/env python3
"""実通知UI・HTTP・共有Gate・独立MCP fixtureの適合検証。実外部サービス受入とは分ける。"""
import asyncio
import json
import os
import shutil
import hashlib
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from tests.addon_events_test_support import connected, profile
from tests.external_mcp_test_support import manifest
from app.external_mcp.models import digest
from acceptance_addon_admin import port, process, ready


async def prepare(work):
    async with connected(work / "provider") as (_, gate, _):
        source = profile(gate)
        snapshot = gate.registry.entry(source.connection_id).active or gate.registry.entry(source.connection_id).staged
        detail = next(t for t in snapshot.document["tools"] if t["name"] == "detail")
        def operation(value):
            return {"kind": value.kind, "ref": value.ref, "definition_digest": value.definition_digest}
        source_json = {"id": source.id, "connection_id": source.connection_id, "character_id": source.character_id,
                       "history": operation(source.history), "snapshot": operation(source.snapshot), "limits": {"poll_seconds": 3}}
        reg = {"id": "conformance-rule", "source_id": source.id, "character_id": "miori", "event_type": "task.completed",
               "decision": "notify", "detail": {"kind": "tool", "ref": "detail", "definition_digest": digest(detail["native_definition"])},
               "reference_arguments": {"task_ref": "task_ref"}}
    value = manifest(trusted=True)
    value["connection"]["stdio"] = {"command": sys.executable, "args": [
        str(ROOT / "backend/tests/fixtures/addon_events/server.py"), "--state", str(work / "provider/state.json"),
        "--calls", str(work / "provider/calls.jsonl"), "--pid", str(work / "provider/pid")]}
    (work / "mcp.json").write_text(json.dumps({"version": 1, "connections": [value]}))
    (work / "events.json").write_text(json.dumps({"version": 1, "sources": [source_json]}))
    (work / "notifications.json").write_text(json.dumps({"version": 1, "registrations": [reg],
                                                      "limits": {"poll_seconds": 3, "max_per_user": 2}}))


def source_hash():
    digest_value = hashlib.sha256()
    inputs = [*sorted((ROOT / "backend/app").rglob("*.py")), *sorted((ROOT / "frontend/src").rglob("*.svelte")),
              *sorted((ROOT / "frontend/src").rglob("*.ts")), Path(__file__),
              ROOT / "backend/tests/fixtures/addon_events/server.py",
              ROOT / "backend/tests/fixtures/notifications/application.py",
              ROOT / "frontend/scripts/acceptance-notifications.mjs"]
    for path in inputs:
        digest_value.update(str(path.relative_to(ROOT)).encode() + bytes([0]) + path.read_bytes())
    return digest_value.hexdigest()


def main():
    if os.environ.get("DS_ENVIRONMENT_ID") == "dogfood":
        raise RuntimeError("dogfood is not an acceptance environment")
    source_digest = source_hash()
    output = ROOT / "docs/artifacts/notification-183"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ds-notification-183-") as temporary, ExitStack() as stack:
        work = Path(temporary)
        asyncio.run(prepare(work))
        backend_url, frontend_url = "http://127.0.0.1:" + str(port()), "http://127.0.0.1:" + str(port())
        environment = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "FONTCONFIG_FILE", "FONTCONFIG_PATH"}}
        font = next((Path(p) for p in (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/mnt/c/Windows/Fonts/meiryo.ttc"
        ) if Path(p).is_file()), None)
        if font is not None:
            fonts = work / "fonts"
            fonts.mkdir()
            shutil.copy2(font, fonts / font.name)
            (work / "fonts.conf").write_text('<fontconfig><dir>' + str(fonts) + '</dir><cachedir>' + str(work / "font-cache") + '</cachedir></fontconfig>')
            environment["FONTCONFIG_FILE"] = str(work / "fonts.conf")
        environment.update(PYTHONPATH=str(ROOT / "backend"), PYTHON_DOTENV_DISABLED="1", DS_ENVIRONMENT_ID="test",
                           DS_DATA_DIR=str(work / "data"), DS_NOTIFICATION_CONFORMANCE_ROOT=str(work),
                           DS_MCP_CONFIG=str(work / "mcp.json"), DS_MCP_EVENT_CONFIG=str(work / "events.json"),
                           DS_NOTIFICATION_CONFIG=str(work / "notifications.json"))
        backend = stack.enter_context(process([sys.executable, "-m", "uvicorn", "tests.fixtures.notifications.application:app",
            "--host", "127.0.0.1", "--port", backend_url.rsplit(":", 1)[1]], environment, ROOT, work / "backend.log"))
        try:
            ready(backend_url + "/notifications", backend)
        except Exception:
            print((work / "backend.log").read_text()[-6000:])
            raise
        report = {"reportSchemaVersion": 1, "effectiveProfile": "notification-conformance", "capabilities": [],
                  "readyGate": {"baseUrl": backend_url, "host": "127.0.0.1", "port": int(backend_url.rsplit(":", 1)[1])},
                  "dependencies": {name: ({"mode": "real", "source": "external", "baseUrl": url}
                      if url else {"mode": "disabled", "source": None})
                      for name, url in {"backend": backend_url, "frontend": frontend_url, "ollama": None,
                                        "voicevox": None, "whisper": None, "chroma": None}.items()}}
        (work / "profile.json").write_text(json.dumps(report))
        frontend = stack.enter_context(process(["node", str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
            "--host", "127.0.0.1", "--port", frontend_url.rsplit(":", 1)[1], "--strictPort"],
            environment | {"DS_PROFILE_REPORT": str(work / "profile.json"), "DS_BACKEND_ORIGIN": backend_url},
            ROOT / "frontend", work / "frontend.log"))
        ready(frontend_url, frontend)
        browser = subprocess.run(["node", str(ROOT / "frontend/scripts/acceptance-notifications.mjs"), frontend_url,
                                  str(work), str(output)], cwd=ROOT / "frontend", env=environment, capture_output=True, text=True, timeout=150)
        if browser.returncode:
            print(browser.stdout[-3000:])
            print(browser.stderr[-4000:])
            print((work / "backend.log").read_text()[-4000:])
            raise RuntimeError("notification browser conformance failed")
        evidence = json.loads(browser.stdout.strip().splitlines()[-1])
        if source_digest != source_hash():
            raise RuntimeError("source changed during browser conformance")
        evidence.update(source_sha256=source_digest, working_tree=True)
        evidence.update(scope="browser-http-gate-independent-mcp-fixture", llm_configured=False,
                        production_external_service=False, commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip())
        (output / "browser-conformance.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
