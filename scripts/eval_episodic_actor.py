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
    parser.add_argument("--design", choices=["production", "compact", "legacy"], default="production")
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
        "EPISODIC_EVAL_DESIGN": args.design,
        "PYTHON_DOTENV_DISABLED": "1", "DS_ENVIRONMENT_ID": "test",
        "PROMPTFOO_DISABLE_TELEMETRY": "1", "PROMPTFOO_DISABLE_WAL_MODE": "1",
        "PROMPTFOO_PASS_RATE_THRESHOLD": "0", "PROMPTFOO_PYTHON": sys.executable,
        "PROMPTFOO_CONFIG_DIR": str(output / "promptfoo-config"),
        "EPISODIC_ACTOR_EVAL_PROGRESS": str(output / "progress.jsonl"),
        "PYTHONPATH": str(ROOT / "backend"),
    })
    # 共通Inference設定は未使用Targetも必須。評価で未指定の分だけローカル値を補う。
    for name, output_tokens in (("CHAT", 1024), ("PRIVACY", 512),
                                ("MEMORY_EXTRACTION", 4096), ("MEMORY_CONSOLIDATION", 512)):
        environment.setdefault(f"INFERENCE_TARGET_{name}", "ollama/gemma4:e4b")
        environment.setdefault(f"INFERENCE_TARGET_{name}_MAX_INPUT_TOKENS", str(36864 - output_tokens))
        environment.setdefault(f"INFERENCE_TARGET_{name}_MAX_OUTPUT_TOKENS", str(output_tokens))
    environment.setdefault("INFERENCE_TARGET_EMBEDDING", "ollama/nomic-embed-text:latest")
    environment.setdefault("INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS", "8192")
    # 設定・モデル準備の失敗を100件のモデル精度へ混ぜない。
    sys.path.insert(0, str(ROOT / "backend"))
    from app.inference import InferenceTarget
    from app.inference.runtime import create_inference_runtime
    inference = create_inference_runtime(environment)
    try:
        reference = inference.settings.target(InferenceTarget.MEMORY_EXTRACTION).reference
        if reference.provider_id != "ollama":
            raise ValueError("actor evaluation requires the local Ollama target")
        digest = inference.ollama_adapter.resolve_model_digest(reference.model_id, timeout_seconds=30)
    finally:
        inference.close()
    manifest = {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "design": args.design,
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / "backend/app/memory/formation" / name).read_bytes()).hexdigest()
            for name in ("episodic_extractor.py", "compact_extractor.py", "ground_schema.py")
        },
        "scope": "Fact grounding actor accuracy; no extraction selection or privacy persistence",
        "cases_sha256": hashlib.sha256((SUITE / "cases.jsonl").read_bytes()).hexdigest(),
        "extractor_sha256": hashlib.sha256((ROOT / "backend/app/memory/formation/episodic_extractor.py").read_bytes()).hexdigest(),
        "model_id": reference.model_id, "model_digest": digest,
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
