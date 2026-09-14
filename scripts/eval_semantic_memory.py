"""固定100ケースを本番保存経路でモデルごとに3回評価する。"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "backend/evals/semantic_memory"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=["gemma4:e4b", "gemma4:12b"])
    parser.add_argument("--endpoint", default="http://localhost:11438")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=int, default=100, help="部分実行は調査専用、正式合格には使用しない")
    args = parser.parse_args()
    url = urlparse(args.endpoint)
    if url.hostname not in {"localhost", "127.0.0.1", "::1"} or url.port in {14174, 18000, 15173}:
        raise ValueError("evaluation requires a local dedicated dev/test inference endpoint")
    if not 1 <= args.runs <= 3 or not 1 <= args.limit <= 100:
        raise ValueError("invalid run count or case limit")
    output = (args.output_dir or Path(tempfile.mkdtemp(prefix="ds341-evaluation-"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("evidence directory must be empty")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("INFERENCE_TARGET_")}
    environment.update({
        "OLLAMA_BASE_URL": args.endpoint, "DS_ENVIRONMENT_ID": "test", "PYTHON_DOTENV_DISABLED": "1",
        "PROMPTFOO_DISABLE_TELEMETRY": "1", "PROMPTFOO_DISABLE_WAL_MODE": "1",
        "PROMPTFOO_PASS_RATE_THRESHOLD": "0", "PROMPTFOO_PYTHON": sys.executable,
        "PROMPTFOO_CONFIG_DIR": str(output / "promptfoo-config"), "PYTHONPATH": str(ROOT / "backend"),
    })
    for name, tokens in (("CHAT", 1024), ("PRIVACY", 512), ("MEMORY_EXTRACTION", 4096),
                         ("MEMORY_CONSOLIDATION", 512), ("SEMANTIC_EXTRACTION", 4096)):
        # 比較対象は意味抽出のみ。privacyも同じe4b・共通設定に固定する。
        model = args.model if name == "SEMANTIC_EXTRACTION" else "gemma4:e4b"
        environment[f"INFERENCE_TARGET_{name}"] = "ollama/" + model
        environment[f"INFERENCE_TARGET_{name}_MAX_INPUT_TOKENS"] = "32768" if name == "SEMANTIC_EXTRACTION" else "7680"
        environment[f"INFERENCE_TARGET_{name}_MAX_OUTPUT_TOKENS"] = str(tokens)
        environment[f"INFERENCE_TARGET_{name}_TIMEOUT_SECONDS"] = "120"
        environment[f"INFERENCE_TARGET_{name}_MAX_CONCURRENCY"] = "1"
        environment[f"INFERENCE_TARGET_{name}_OPTIONS_JSON"] = '{"temperature":0,"think":false}'
    environment["INFERENCE_TARGET_EMBEDDING"] = "ollama/nomic-embed-text:latest"
    environment["INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS"] = "8192"
    if environment.get("PRIVACY_EVAL_STUB"):
        raise ValueError("real evaluation forbids stubs")
    with urlopen(args.endpoint + "/api/tags", timeout=10) as response:
        models = {m["name"]: m for m in json.load(response)["models"]}
    if args.model not in models or "gemma4:e4b" not in models:
        raise ValueError("comparison and privacy models must be installed before evaluation")
    with urlopen(args.endpoint + "/api/version", timeout=10) as response:
        version = json.load(response)
    cases = [json.loads(line) for line in (SUITE / "cases.jsonl").read_text().splitlines()][:args.limit]
    tests = [{"vars": {"case_id": c["id"], "category": c["category"],
                       "input_json": json.dumps({k: c[k] for k in ("turns", "existing")}, ensure_ascii=False),
                       "expected_json": json.dumps(c["expected"], ensure_ascii=False)}} for c in cases]
    config = {"description": "意味記憶の本番保存経路評価", "prompts": ["{{input_json}}"],
              "providers": [{"id": "file://" + str(SUITE / "provider.py"), "label": args.model}],
              "evaluateOptions": {"maxConcurrency": 1},
              "defaultTest": {"assert": [{"type": "python", "metric": "semantic_accuracy",
                  "value": "from evals.semantic_memory.score import get_assert\nreturn get_assert(output, context)"}]},
              "tests": tests}
    config_path = output / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    tracked = [*SUITE.glob("*.py"), SUITE / "cases.jsonl",
               * (ROOT / "backend/app/memory/semantic").glob("*.py")]
    manifest = {
        "model": models[args.model], "privacy_model": models["gemma4:e4b"], "ollama": version,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
        "files_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked},
        "inference_settings": {k: v for k, v in environment.items() if k.startswith("INFERENCE_TARGET_")},
        "cache": False, "case_count": len(cases), "runs": args.runs,
        "acceptance_eligible": args.runs == 3 and len(cases) == 100,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    sys.path.insert(0, str(ROOT / "backend"))
    from evals.semantic_memory.score import score
    print(f"評価証跡: {output}", flush=True)
    summaries = []
    for run in range(1, args.runs + 1):
        progress = output / f"run-{run}.jsonl"
        environment["SEMANTIC_EVAL_PROGRESS"] = str(progress)
        with (output / f"run-{run}.log").open("w") as log:
            result = subprocess.run([str(ROOT / "node_modules/.bin/promptfoo"), "eval",
                "--config", str(config_path), "--no-cache", "--max-concurrency", "1", "--no-table",
                "--output", str(output / f"run-{run}.json")],
                cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, check=False)
        rows = [json.loads(line) for line in progress.read_text().splitlines()] if progress.exists() else []
        by_id = {r["case_id"]: r for r in rows}
        categories = defaultdict(list)
        failures, forbidden = {}, []
        for case in cases:
            row = by_id.get(case["id"], {"error_type": "MissingResult"})
            assessment = score(row, case["expected"])
            categories[case["category"]].append(assessment["pass"])
            if not assessment["pass"]:
                failures[case["id"]] = assessment["reason"]
            if row.get("foreign_modified") or (
                case["expected"].get("forbidden_save") and row.get("committed")
            ) or any(r["character_id"] != "miori" for r in row.get("saved", [])):
                forbidden.append(case["id"])
        rates = {key: sum(values) / len(values) for key, values in categories.items()}
        passed = (result.returncode == 0 and len(rows) == len(cases) and len(by_id) == len(cases)
                  and not forbidden and all(rate >= 0.9 for rate in rates.values()))
        summary = {"run": run, "passed": passed, "category_accuracy": rates,
                   "forbidden_cases": forbidden, "failures": failures, "cli_exit": result.returncode}
        summaries.append(summary)
        (output / "summary.json").write_text(json.dumps({
            "acceptance_eligible": manifest["acceptance_eligible"], "runs": summaries,
            "passed": manifest["acceptance_eligible"] and len(summaries) == 3 and all(s["passed"] for s in summaries),
        }, ensure_ascii=False, indent=2))
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if all(s["passed"] for s in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
