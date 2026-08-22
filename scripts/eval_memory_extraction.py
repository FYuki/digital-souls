from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = REPOSITORY_ROOT / "backend" / "evals" / "memory_extraction"
CONFIG_PATHS = {
    "conformance": EVAL_ROOT / "conformance.yaml",
    "prompt-lab": EVAL_ROOT / "prompt-lab.yaml",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=sorted(CONFIG_PATHS))
    arguments, promptfoo_arguments = parser.parse_known_args()
    artifact_directory = Path(tempfile.mkdtemp(prefix="memory-extraction-eval-"))
    output_path = artifact_directory / "results.json"
    environment = os.environ.copy()
    environment["PROMPTFOO_DISABLE_TELEMETRY"] = "1"
    environment["PROMPTFOO_DISABLE_WAL_MODE"] = "1"
    environment["PROMPTFOO_PASS_RATE_THRESHOLD"] = "0"
    environment["PROMPTFOO_CONFIG_DIR"] = str(
        artifact_directory / "promptfoo-config"
    )
    backend_path = str(REPOSITORY_ROOT / "backend")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        os.pathsep.join((backend_path, existing_pythonpath))
        if existing_pythonpath
        else backend_path
    )
    try:
        try:
            evaluation = subprocess.run(
                [
                    "promptfoo",
                    "eval",
                    "--config",
                    str(CONFIG_PATHS[arguments.mode]),
                    "--no-cache",
                    "--output",
                    str(output_path),
                    *promptfoo_arguments,
                ],
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
            )
        except FileNotFoundError:
            print(
                "Promptfoo CLIが見つかりません。リポジトリルートでnpm ciを実行してください。",
                file=sys.stderr,
            )
            return 127
        if evaluation.returncode != 0:
            return evaluation.returncode
        gate_command = [
            sys.executable,
            str(EVAL_ROOT / "gate.py"),
            str(output_path),
        ]
        return subprocess.run(
            gate_command,
            cwd=REPOSITORY_ROOT,
            check=False,
        ).returncode
    finally:
        shutil.rmtree(artifact_directory)


if __name__ == "__main__":
    raise SystemExit(main())
