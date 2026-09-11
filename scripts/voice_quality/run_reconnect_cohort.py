"""実network障害を独立sessionで逐次測定し、失敗も残して全件を匿名再集計する。"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from network_fault import resolve_target
from run_pilot import ROOT, measurement_revision, run_root


def planned_runs(cohort_id: str, sessions: int) -> list[str]:
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}", cohort_id)
            or isinstance(sessions, bool) or not isinstance(sessions, int) or not 1 <= sessions <= 100):
        raise ValueError("invalid reconnect cohort plan")
    return [f"{cohort_id}-{index:03d}" for index in range(1, sessions + 1)]


def verify_trial(run_id: str, revision: str) -> dict[str, bool]:
    base = run_root(run_id)
    manifest = json.loads((base / "trial-manifest.json").read_text())
    environment = json.loads((base / "runtime-data/runtime/standalone/environment-run.json").read_text())
    if (manifest.get("measurement_scope") != "livekit_fault_recovery_session_diagnostic"
            or manifest.get("measurement_revision") != revision
            or manifest.get("fault_clock_process_closed") is not True
            or environment.get("runtime", {}).get("environmentId") != "test"
            or environment.get("runtime", {}).get("dataRoot") != str((base / "runtime-data").resolve())
            or environment.get("effectiveProfile", {}).get("effectiveProfile") != "integration-voice-fault"
            or environment.get("teardown", {}).get("status") != "completed"):
        raise ValueError("trial ownership, revision, or teardown unavailable")
    native = json.loads((base / 'native-sdk.json').read_text())
    if native.get('status') != 'verified' or not isinstance(native.get('build'), dict):
        raise ValueError('trial native SDK not verified')
    for name in ("frontend", "backend"):
        service = environment.get("services", {}).get(name, {})
        container_id = service.get("containerIdentity", {}).get("containerId", "")
        if service.get("owned") is not True or not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise ValueError("owned trial container unavailable")
        result = subprocess.run(["docker", "inspect", container_id], capture_output=True,
                                text=True, timeout=15, check=False)
        if result.returncode == 0 or not any(reason in result.stderr.lower()
                for reason in ("no such object", "no such container")):
            raise ValueError("trial container deletion unverified")
    # 合否はここでは参考値。最終集計は全rawの時計・実出力から再計算する。
    return {"session_end_confirmed": manifest.get("session_end_confirmed") is True,
            "reported_success": manifest.get("outcome") == "success", "owned_containers_deleted": True}


def run_trial(run_id: str, args: argparse.Namespace, log_path: Path) -> int:
    command = [sys.executable, str(ROOT / "scripts/voice_quality/run_pilot.py"),
        "--run-id", run_id, "--inference-env", str(args.inference_env), "--livekit-env", str(args.livekit_env),
        "--fault-bridge", "--network-fault", "--control-probe", "--trials", "3", "--scheduled-fixture"]
    if args.disable_thinking:
        command.append("--disable-thinking")
    with log_path.open("x") as output:
        child = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return child.wait(timeout=300)
        except BaseException:
            # runnerとPlaywrightを同じ所有process groupで止め、後片付けを待つ。
            # 他の試行や共有serviceのprocessにはsignalを送らない。
            for sig, timeout in ((signal.SIGINT, 45), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=timeout)
                    break
                except subprocess.TimeoutExpired:
                    continue
            raise


def aggregate(run_ids: list[str], output: Path) -> int:
    result = subprocess.run(["node", str(ROOT / "frontend/scripts/report-fault-recovery.mjs"),
        "--expected", str(len(run_ids)), "--output", str(output), "--require-native-sdk",
        *[str(run_root(run_id) / "trial-manifest.json") for run_id in run_ids]],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode not in (0, 1) or not output.is_file():
        raise ValueError("cohort report unavailable")
    return result.returncode


def execute(args: argparse.Namespace) -> int:
    runs = planned_runs(args.cohort_id, args.sessions)
    revision = measurement_revision(ROOT)
    # LiveKitは呼び出し側が専用bridgeで起動する。ここでは共有serviceを起動・停止しない。
    target = resolve_target("ds-voice-quality-fault-livekit-1")
    parent = ROOT / "frontend/test-results/livekit-quality/cohorts"
    parent.mkdir(parents=True, exist_ok=True)
    with (parent / ".execution.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any(run_root(run_id).exists() for run_id in runs):
            raise ValueError("planned run already exists")
        directory = parent / args.cohort_id
        directory.mkdir(exist_ok=False)
        with (directory / "plan.json").open("x") as output:
            json.dump({"measurement_revision": revision, "expected_sessions": len(runs), "run_ids": runs,
                       "scheduled_fixture": True, "thinking_disabled": args.disable_thinking}, output, indent=2)
            output.write("\n")
        with (directory / "execution.jsonl").open("x") as journal:
            def record(value: dict[str, Any]) -> None:
                journal.write(json.dumps(value, allow_nan=False) + "\n")
                journal.flush()
                print(json.dumps(value, allow_nan=False), flush=True)

            completed = 0
            try:
                for index, run_id in enumerate(runs, 1):
                    if (directory / "stop-requested").exists():
                        record({"event": "cohort_stopped", "completed": completed, "reason": "stop_requested"})
                        return 2
                    if measurement_revision(ROOT) != revision:
                        raise ValueError("measurement revision changed")
                    if resolve_target("ds-voice-quality-fault-livekit-1") != target:
                        raise ValueError("dedicated target changed")
                    record({"event": "trial_started", "index": index, "expected": len(runs)})
                    code = run_trial(run_id, args, directory / f"trial-{index:03d}.log")
                    checks = verify_trial(run_id, revision)
                    completed += 1
                    record({"event": "trial_completed", "index": index, "exit_code": code, **checks})
                code = aggregate(runs, directory / "report.json")
                record({"event": "cohort_report_created", "completed": completed, "passed": code == 0})
                return code
            except BaseException:
                # 失敗した試行を削除・再実行しない。曖昧な後片付けで次の環境を重ねない。
                record({"event": "cohort_stopped", "completed": completed, "reason": "execution_or_teardown_unverified"})
                raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--sessions", type=int, default=100, help="独立session数。100未満は診断のみ。")
    parser.add_argument("--inference-env", type=Path, required=True)
    parser.add_argument("--livekit-env", type=Path, required=True)
    parser.add_argument("--disable-thinking", action="store_true")
    try:
        return execute(parser.parse_args())
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError):
        print("再接続cohortを停止しました。実行記録と所有環境の終了状態を確認してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
