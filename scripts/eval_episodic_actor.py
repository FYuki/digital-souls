"""導入済みpromptfooで行為者の正解率を測る。証跡は削除せず専用出力先へ残す。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "backend/evals/episodic_actor"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if os.environ.get("DS_ENVIRONMENT_ID") == "dogfood":
        raise RuntimeError("actor evaluation requires a dev/test environment")
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="episodic-actor-eval-"))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("output directory must be empty; previous evidence is preserved")
    cli = shutil.which("promptfoo") or str(ROOT / "node_modules/.bin/promptfoo")
    if not Path(cli).is_file():
        raise RuntimeError("promptfoo is unavailable; run npm ci at repository root")
    environment = os.environ.copy()
    environment.update({
        "PYTHON_DOTENV_DISABLED": "1", "DS_ENVIRONMENT_ID": "test",
        "PROMPTFOO_DISABLE_TELEMETRY": "1", "PROMPTFOO_DISABLE_WAL_MODE": "1",
        "PROMPTFOO_PASS_RATE_THRESHOLD": "0", "PROMPTFOO_PYTHON": sys.executable,
        "PROMPTFOO_CONFIG_DIR": str(output / "promptfoo-config"),
        "EPISODIC_ACTOR_EVAL_PROGRESS": str(output / "progress.jsonl"),
        "PYTHONPATH": str(ROOT / "backend"),
    })
    manifest = {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "scope": "production Fact grounding actor accuracy; no extraction selection or privacy persistence",
        "cases_sha256": hashlib.sha256((SUITE / "cases.jsonl").read_bytes()).hexdigest(),
        "extractor_sha256": hashlib.sha256((ROOT / "backend/app/memory/formation/episodic_extractor.py").read_bytes()).hexdigest(),
        "cache": False, "max_concurrency": 1, "threshold": .9, "case_count": 100,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"評価証跡: {output}", flush=True)
    with (output / "promptfoo.log").open("w") as log:
        evaluation = subprocess.run([
            cli, "eval", "--config", str(SUITE / "conformance.yaml"), "--no-cache",
            "--max-concurrency", "1", "--no-table", "--output", str(output / "results.json"),
        ], cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, check=False)
    # 個別の不正解は90% gateで判定する。CLI障害や結果欠落は合格にしない。
    if evaluation.returncode != 0 or not (output / "results.json").exists():
        print(f"promptfoo実行失敗: exit={evaluation.returncode}; {output / 'promptfoo.log'}")
        return evaluation.returncode or 1
    return subprocess.run([
        sys.executable, str(SUITE / "gate.py"), str(output / "results.json"),
        "--summary", str(output / "summary.json"),
    ], cwd=ROOT, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
