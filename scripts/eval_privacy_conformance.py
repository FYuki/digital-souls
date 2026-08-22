from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = REPOSITORY_ROOT / "backend" / "evals" / "privacy_classifier"
CONFIG_PATHS = {
    "conformance": EVAL_ROOT / "conformance.yaml",
    "prompt-lab": EVAL_ROOT / "prompt-lab.yaml",
}
CASES_PATH = EVAL_ROOT / "cases.jsonl"
THRESHOLDS_PATH = EVAL_ROOT / "thresholds.json"
GATE_PATH = EVAL_ROOT / "gate.py"
PROTECTED_PROMPTFOO_OPTIONS = {
    "--config",
    "-c",
    "--no-write",
    "--cache",
    "--no-cache",
    "--output",
    "-o",
    "--max-concurrency",
    "--share",
    "--no-share",
}


def parse_args() -> tuple[str, int | None, list[str]]:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=sorted(CONFIG_PATHS))
    parser.add_argument("-n", "--filter-first-n", type=int)
    arguments, promptfoo_arguments = parser.parse_known_args()
    if arguments.filter_first_n is not None and arguments.filter_first_n < 1:
        parser.error("--filter-first-n must be a positive integer")
    for argument in promptfoo_arguments:
        option = argument.split("=", maxsplit=1)[0]
        if option in PROTECTED_PROMPTFOO_OPTIONS:
            parser.error(f"{option} is controlled by the privacy evaluation wrapper")
    return arguments.mode, arguments.filter_first_n, promptfoo_arguments


def _promptfoo_command(
    mode: str,
    output_path: Path,
    filter_first_n: int | None,
    extra_arguments: list[str],
) -> list[str]:
    return [
        "promptfoo",
        "eval",
        "--config",
        str(CONFIG_PATHS[mode]),
        "--no-cache",
        "--no-write",
        "--max-concurrency",
        "1",
        "--output",
        str(output_path),
        *(
            ["--filter-first-n", str(filter_first_n)]
            if filter_first_n is not None
            else []
        ),
        *extra_arguments,
    ]


def _gate_command(
    mode: str,
    output_path: Path,
    filter_first_n: int | None,
) -> list[str]:
    command = [
        sys.executable,
        str(GATE_PATH),
        "--results",
        str(output_path),
        "--cases",
        str(CASES_PATH),
        "--thresholds",
        str(THRESHOLDS_PATH),
    ]
    if filter_first_n is not None:
        command.extend(("--filter-first-n", str(filter_first_n)))
    if mode == "prompt-lab":
        command.append("--report-only")
    return command


def run(
    mode: str,
    filter_first_n: int | None,
    promptfoo_arguments: list[str],
) -> int:
    keep_artifacts = os.environ.get("PRIVACY_EVAL_KEEP_ARTIFACTS") == "1"
    artifact_directory = Path(tempfile.mkdtemp(prefix="privacy-eval-"))
    output_path = artifact_directory / "results.jsonl"
    environment = os.environ.copy()
    environment["PROMPTFOO_DISABLE_TELEMETRY"] = "1"
    environment["PROMPTFOO_CONFIG_DIR"] = str(
        artifact_directory / "promptfoo-config"
    )
    backend_pythonpath = str(REPOSITORY_ROOT / "backend")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        os.pathsep.join((backend_pythonpath, existing_pythonpath))
        if existing_pythonpath
        else backend_pythonpath
    )
    try:
        try:
            promptfoo = subprocess.run(
                _promptfoo_command(
                    mode,
                    output_path,
                    filter_first_n,
                    promptfoo_arguments,
                ),
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
        if promptfoo.returncode != 0:
            return promptfoo.returncode
        gate = subprocess.run(
            _gate_command(mode, output_path, filter_first_n),
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        return gate.returncode
    finally:
        if keep_artifacts:
            print(f"privacy evaluation artifacts: {artifact_directory}")
        else:
            shutil.rmtree(artifact_directory)


def main() -> int:
    mode, filter_first_n, promptfoo_arguments = parse_args()
    return run(mode, filter_first_n, promptfoo_arguments)


if __name__ == "__main__":
    raise SystemExit(main())
