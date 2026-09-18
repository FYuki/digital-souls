"""記憶形成を維持し、新規data rootごとの通常音声を既存runner・reporterで測る。"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from run_pilot import ROOT, measurement_revision, run_root


def plan(cohort_id: str, measured: int) -> list[tuple[str, str]]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,55}", cohort_id):
        raise ValueError("unsafe cohort identifier")
    if type(measured) is not int or not 1 <= measured <= 100:
        raise ValueError("invalid measured count")
    return [(f"{cohort_id}-{i + 1:03d}", "warmup" if i < 5 else "measured")
            for i in range(5 + measured)]


def verify_stopped(base: Path, revision: str, profile: str = "integration-voice") -> dict:
    if profile not in ("integration-voice", "integration-irodori"):
        raise ValueError("unsupported independent normal profile")
    report = json.loads((base / "runtime-data/runtime/standalone/environment-run.json").read_text())
    if (report.get("runtime", {}).get("environmentId") != "test"
            or report["runtime"].get("dataRoot") != str((base / "runtime-data").resolve())
            or report.get("teardown", {}).get("status") != "completed"
            or report.get("effectiveProfile", {}).get("effectiveProfile") != profile):
        raise ValueError("owned test environment teardown not verified")
    native = json.loads((base / "native-sdk.json").read_text())
    if native.get("status") != "verified":
        raise ValueError("native SDK not verified")
    for service in ("backend", "frontend"):
        row = report["services"][service]
        cid = row.get("containerIdentity", {}).get("containerId", "")
        if row.get("owned") is not True or not re.fullmatch(r"[0-9a-f]{64}", cid):
            raise ValueError("owned container identity unavailable")
        probe = subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, timeout=15, check=False)
        if probe.returncode == 0 or not any(s in probe.stderr.lower() for s in ("no such object", "no such container")):
            raise ValueError("owned container deletion not verified")
    manifest = json.loads((base / "trial-manifest.json").read_text())
    if manifest.get("measurement_revision") != revision or manifest.get("measurement_scope") != "isolated_normal_trial":
        raise ValueError("isolated trial revision not verified")
    return manifest


def trial(run_id: str, phase: str, args, revision: str, directory: Path) -> tuple[int, dict]:
    command = [sys.executable, str(ROOT / "scripts/voice_quality/run_pilot.py"),
               "--run-id", run_id, "--inference-env", str(args.inference_env),
               "--livekit-env", str(args.livekit_env), "--trials", "1",
               "--scheduled-fixture", "--isolated-normal-phase", phase, "--profile", args.profile]
    if args.disable_thinking:
        command.append("--disable-thinking")
    images = {}
    base = run_root(run_id)
    with (directory / f"{run_id}.log").open("x") as log:
        child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 300
        try:
            while child.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError("isolated trial deadline")
                path = base / "runtime-data/runtime/standalone/environment-run.json"
                if path.exists():
                    try:
                        report = json.loads(path.read_text())
                    except json.JSONDecodeError:
                        report = {}
                    for service in ("backend", "frontend"):
                        row = report.get("services", {}).get(service, {})
                        cid = (row.get("containerIdentity") or {}).get("containerId")
                        if service in images or row.get("owned") is not True or not cid:
                            continue
                        found = subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, timeout=5, check=False)
                        if found.returncode:
                            continue
                        item = json.loads(found.stdout)[0]
                        if not item["State"]["Running"]:
                            continue
                        if item["Id"] != cid or item["Config"]["Image"] != f"digital-souls-voice-quality/{service}:{revision}":
                            raise ValueError("unexpected running image")
                        images[service] = {"image_id": item["Image"], "image_tag": item["Config"]["Image"]}
                time.sleep(0.5)
            code = child.wait()
        except BaseException:
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
    manifest = verify_stopped(base, revision, args.profile)
    rows = manifest.get("trials", [])
    if len(rows) != 1 or rows[0].get("phase") != phase:
        raise ValueError("one isolated trial required")
    return code, {"outcome": rows[0].get("outcome"), "session_end_confirmed": rows[0].get("session_end_confirmed") is True,
                  "running_images": images, "manifest_sha256": hashlib.sha256((base / "trial-manifest.json").read_bytes()).hexdigest()}


def preparation_summary(trials: list[dict]) -> dict:
    """準備欠測・失敗・複数操作を残す。準備時間をTTFAへ混ぜない。"""
    durations: list[float] = []
    counts = {"ready": 0, "not_ready": 0, "missing": 0, "invalid": 0}
    for trial in trials:
        row = trial.get("preparation_observation")
        if not isinstance(row, dict):
            counts["missing"] += 1
            continue
        started, ready, duration = (row.get(key) for key in
                                    ("started_client_ms", "ready_client_ms", "duration_ms"))
        def valid_number(value):
            return type(value) in (int, float) and math.isfinite(value) and value >= 0
        if (row.get("method") != "browser_click_to_microphone_standby_dom_v1"
                or row.get("clock_domain") != "browser_performance"
                or type(row.get("attempt_count")) is not int or row["attempt_count"] != 1
                or not valid_number(started)):
            counts["invalid"] += 1
        elif ready is None and duration is None:
            counts["not_ready"] += 1
        elif (not valid_number(ready) or not valid_number(duration) or ready < started
                or not math.isclose(ready - started, duration, abs_tol=0.001)):
            counts["invalid"] += 1
        else:
            counts["ready"] += 1
            durations.append(duration)
    durations.sort()
    def percentile(fraction):
        return durations[max(0, math.ceil(len(durations) * fraction) - 1)] if durations else None
    return {
        "denominator": len(trials), **counts,
        "coverage": len(durations) / len(trials) if trials else 0,
        "duration_ms_p50": percentile(0.5), "duration_ms_p95": percentile(0.95),
        "duration_ms_max": max(durations) if durations else None,
        "method": "browser_click_to_microphone_standby_dom_v1",
        "note": "DOM反映時刻による準備完了観測。OS権限待ちと接続・入力認可を含む。TTFAとは別。",
    }


def aggregate(runs: list[tuple[str, str]], directory: Path, revision: str, measured: int) -> int:
    manifests = [json.loads((run_root(r) / "trial-manifest.json").read_text()) for r, _ in runs]
    combined = dict(manifests[0])
    combined.update(measurement_scope="controlled", expected_warmup=5, expected_measured=measured,
                    trials=[m["trials"][0] for m in manifests])
    # 片側の初期状態を都合よく補完しない。各試行の証拠は既存reporterで再検証する。
    hashes = {m.get("initial_state_hash") for m in manifests if m.get("initial_state_hash")}
    if len(hashes) != 1:
        raise ValueError("initial state differs across isolated trials")
    combined["initial_state_hash"] = next(iter(hashes))
    combined["isolated_run_manifest_sha256"] = {
        r: hashlib.sha256((run_root(r) / "trial-manifest.json").read_bytes()).hexdigest() for r, _ in runs}
    target = directory / "trial-manifest.json"
    target.write_text(json.dumps(combined, indent=2) + "\n")
    trace = directory / "controlled-trace.jsonl"
    with trace.open("x") as output:
        for run_id, _ in runs:
            output.write((run_root(run_id) / "runtime-data/voice-metrics/controlled-trace.jsonl").read_text())
    summary = {"measurement_revision": revision, "expected_warmup": 5, "expected_measured": measured,
               "recorded_trials": len(combined["trials"]),
               "success": sum(t.get("outcome") == "success" for t in combined["trials"]),
               "failure": sum(t.get("outcome") != "success" for t in combined["trials"]),
               "all_owned_environments_stopped": True, "shared_services_modified": False,
               "all_trial_data_roots_independent": True, "full_acceptance_passed": False}
    code = 1
    if measured == 100:
        with (directory / "reporter.log").open("x") as log:
            code = subprocess.run([sys.executable, "-m", "app.livekit_pilot_report", "--scope", "controlled",
                "--manifest", str(target), "--trace", str(trace), "--output", str(directory / "report.json"),
                "--schema", str(ROOT / "docs/schemas/voice-quality-artifact-v1.schema.json"),
                "--profile-report", str(run_root(runs[0][0]) / "runtime-data/runtime/standalone/resolved-profile.json"),
                "--run-id", directory.name], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
    summary["preparation_by_phase"] = {phase: preparation_summary(
        [trial for trial in combined["trials"] if trial.get("phase") == phase]
    ) for phase in ("warmup", "measured")}
    summary["preparation_evidence_complete"] = all(
        row["ready"] == row["denominator"] and row["denominator"] > 0
        for row in summary["preparation_by_phase"].values()
    )
    summary["existing_reporter_exit"] = code
    summary["resources_note"] = "CPU・memory・GPU観測は各runに保持。複数containerを一つの累積CPU系列へ連結しない。"
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return code if summary["preparation_evidence_complete"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("integration-voice", "integration-irodori"), default="integration-voice")
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--measured", type=int, default=100)
    parser.add_argument("--inference-env", type=Path, required=True)
    parser.add_argument("--livekit-env", type=Path, required=True)
    parser.add_argument("--disable-thinking", action="store_true")
    args = parser.parse_args()
    runs = plan(args.cohort_id, args.measured)
    revision = measurement_revision(ROOT)
    parent = ROOT / "frontend/test-results/livekit-quality/cohorts"
    parent.mkdir(parents=True, exist_ok=True)
    with (parent / ".execution.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any(run_root(r).exists() for r, _ in runs):
            raise ValueError("planned run already exists")
        directory = parent / args.cohort_id
        directory.mkdir(exist_ok=False)
        (directory / "plan.json").write_text(json.dumps({"measurement_revision": revision, "profile": args.profile, "runs": runs,
            "expected_warmup": 5, "expected_measured": args.measured, "data_root_per_trial": True}, indent=2) + "\n")
        with (directory / "execution.jsonl").open("x") as journal:
            for index, (run_id, phase) in enumerate(runs, 1):
                if (directory / "stop-requested").exists() or measurement_revision(ROOT) != revision:
                    return 2
                code, evidence = trial(run_id, phase, args, revision, directory)
                row = {"index": index, "phase": phase, "exit": code, **evidence}
                journal.write(json.dumps(row) + "\n")
                journal.flush()
                print(json.dumps(row), flush=True)
        return aggregate(runs, directory, revision, args.measured)


if __name__ == "__main__":
    raise SystemExit(main())
