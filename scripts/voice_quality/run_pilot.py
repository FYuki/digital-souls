"""独立data rootの実音声pilotと、共有推論環境の数値観測を実行する。"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import ProxyHandler, Request, build_opener

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def run_root(run_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id) is None:
        raise ValueError("run ID must be a single safe identifier")
    return ROOT / "frontend/test-results/livekit-quality/runs" / run_id


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def residency_values(body: object, model: str) -> dict[str, object]:
    """任意文字列、model名、digest、endpointを出力せず、許可した数値だけ残す。"""
    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        return {"outcome": "missing", "reason": "invalid_residency_response"}
    models = body["models"]
    if any(not isinstance(row, dict) for row in models):
        return {"outcome": "missing", "reason": "invalid_residency_response"}
    matches = [row for row in models if row.get("name") == model]
    return {
        "outcome": "observed",
        "resident_model_count": len(models),
        "target_model_present": bool(matches),
        "target_context_tokens": [_number(row.get("context_length")) for row in matches],
        "target_resident_bytes": [_number(row.get("size")) for row in matches],
        "target_vram_bytes": [_number(row.get("size_vram")) for row in matches],
    }


def probe_residency(endpoint: str, model: str) -> dict[str, object]:
    started = time.monotonic_ns()
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request(endpoint.rstrip("/") + "/api/ps"), timeout=1) as response:
            body = response.read(1_000_001)
        if len(body) > 1_000_000:
            result: dict[str, object] = {"outcome": "missing", "reason": "residency_response_too_large"}
        else:
            result = residency_values(json.loads(body), model)
    except Exception:
        # URL、認証情報、providerの応答本文を例外から保存しない。
        result = {"outcome": "missing", "reason": "residency_probe_failed"}
    return {"started_ns": started, "completed_ns": time.monotonic_ns(), **result}


def gpu_values(output: str) -> dict[str, object]:
    devices = []
    try:
        for line in output.strip().splitlines():
            fields = line.split(",")
            if len(fields) != 3:
                raise ValueError("invalid GPU fields")
            busy, used, total = [float(value) for value in fields]
            if any(_number(value) is None for value in (busy, used, total)) or busy > 100 or used > total:
                raise ValueError("invalid GPU range")
            devices.append({"utilization_percent": busy, "used_bytes": round(used * 1024**2),
                            "total_bytes": round(total * 1024**2)})
    except ValueError:
        return {"outcome": "missing", "reason": "gpu_values_unavailable"}
    return ({"outcome": "observed", "scope": "host_gpu", "devices": devices} if devices else
            {"outcome": "missing", "reason": "gpu_values_unavailable"})


def probe_gpu() -> dict[str, object]:
    try:
        result = subprocess.run([
            "nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ], capture_output=True, text=True, timeout=2, check=True)
        return gpu_values(result.stdout)
    except (OSError, subprocess.SubprocessError):
        return {"outcome": "missing", "reason": "gpu_probe_failed"}


def pilot_environment(inference_env: Path, livekit_env: Path, run_id: str,
                      trials: int, disable_thinking: bool, scheduled_fixture: bool = False) -> dict[str, str]:
    run_root(run_id)
    if not inference_env.is_file() or not livekit_env.is_file():
        raise ValueError("pilot environment files are unavailable")
    if not 1 <= trials <= 99:
        raise ValueError("pilot trials must be between 1 and 99")
    excluded = ("INFERENCE_TARGET_HEAVY_REASONING", "INFERENCE_TARGET_VISION")
    env = {k: v for k, v in os.environ.items() if not k.startswith(excluded)}
    for key, value in dotenv_values(inference_env).items():
        if value is not None and key.startswith(("INFERENCE_", "OLLAMA_", "WHISPER_", "VOICEVOX_")) and not key.startswith(excluded):
            env[key] = value
    if disable_thinking:
        options = json.loads(env.get("INFERENCE_TARGET_CHAT_OPTIONS_JSON", "{}"))
        if not isinstance(options, dict):
            raise ValueError("chat options must be an object")
        options["think"] = False
        env["INFERENCE_TARGET_CHAT_OPTIONS_JSON"] = json.dumps(options)
    keys = dotenv_values(livekit_env).get("LIVEKIT_KEYS")
    if keys is None or ":" not in keys:
        raise ValueError("LiveKit keys are unavailable")
    key, secret = keys.split(":", 1)
    if not key.strip() or not secret.strip():
        raise ValueError("LiveKit keys are empty")
    env.update(LIVEKIT_URL="ws://127.0.0.1:7880", LIVEKIT_API_KEY=key.strip(), LIVEKIT_API_SECRET=secret.strip(),
               VOICE_QUALITY_PILOT_TRIALS=str(trials), VOICE_QUALITY_RUN_ID=run_id,
               VOICE_QUALITY_SCHEDULED_FIXTURE="1" if scheduled_fixture else "0")
    return env


def run(args: argparse.Namespace) -> int:
    env = pilot_environment(args.inference_env, args.livekit_env, args.run_id, args.trials, args.disable_thinking, args.scheduled_fixture)
    reference = env.get("INFERENCE_TARGET_CHAT", "")
    if not reference.startswith("ollama/"):
        raise ValueError("this diagnostic requires an Ollama chat target")
    model = reference.split("/", 1)[1]
    endpoint = env.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    expected_context = int(env["INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS"]) + int(env["INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS"])
    base = run_root(args.run_id)
    base.mkdir(parents=True, exist_ok=False)  # 失敗した試行のdata rootも上書きしない。
    process = subprocess.Popen([
        "node", "node_modules/@playwright/test/cli.js", "test", "--config", "playwright.livekit-quality.config.ts",
    ], cwd=ROOT / "frontend", env=env)
    try:
        with (base / "inference-runtime.jsonl").open("x") as output:
            sample = 0
            while process.poll() is None:
                row = {"scope": "pilot_shared_inference_observation", "clock_domain": "observer_monotonic",
                       "expected_context_tokens": expected_context, "thinking_disabled_for_pilot": args.disable_thinking, "scheduled_fixture": args.scheduled_fixture,
                       "ollama": probe_residency(endpoint, model)}
                if sample % 10 == 0:
                    row["gpu"] = probe_gpu()
                output.write(json.dumps(row, allow_nan=False) + "\n")
                output.flush()
                sample += 1
                time.sleep(0.5)
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise
    return process.returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--inference-env", type=Path, required=True)
    parser.add_argument("--livekit-env", type=Path, default=ROOT / "infra/livekit/.env")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--disable-thinking", action="store_true")
    parser.add_argument("--scheduled-fixture", action="store_true")
    raise SystemExit(run(parser.parse_args()))
