from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WRAPPER_PATH = REPOSITORY_ROOT / "scripts" / "eval_privacy_conformance.py"


def _fake_promptfoo_payload(mode: str) -> dict[str, object]:
    if mode == "prompt-lab":
        output = {
            "classification": "NOT_SENSITIVE",
            "subject_scope": "SELF",
            "category": "NONE",
            "reason_code": "NONE",
        }
        return {
            "version": 3,
            "results": {
                "results": [
                    {
                        "provider": {
                            "id": "ollama:chat:gemma4:e4b",
                            "label": "PROMPT_LAB",
                        },
                        "vars": {
                            "text": "私は来月、心臓の検査を受ける予定です。",
                            "case_id": "semantic-01-ja",
                        },
                        "response": {
                            "output": json.dumps(output),
                            "latencyMs": 10,
                        },
                    }
                ]
            },
        }
    records = []
    for profile in ("ADMISSION", "QUERY_GATE"):
        output = {
            "case_id": "semantic-01-ja",
            "profile": profile,
            "classification": "SENSITIVE",
            "subject_scope": "SELF",
            "category": "HEALTH",
            "reason_code": "SEMANTIC_MATCH",
            "classifier_version": "semantic-v1",
            "model_id": "stub-model",
            "model_digest": "sha256:wrapper-fixture",
            "prompt_version": "semantic-v1",
            "policy_version": "wrapper-fixture-policy",
            "latency_seconds": 0.01,
        }
        records.append(
            {
                "provider": {"id": "file://provider.py", "label": profile},
                "vars": {
                    "text": "私は来月、心臓の検査を受ける予定です。",
                    "case_id": "semantic-01-ja",
                },
                "response": {
                    "output": json.dumps(output, ensure_ascii=False),
                    "latencyMs": 10,
                },
            }
        )
    return {"version": 3, "results": {"results": records}}


def _write_fake_promptfoo(bin_dir: Path, payload_path: Path, mode: str) -> Path:
    executable = bin_dir / "promptfoo"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import sys

arguments = sys.argv[1:]
output_option = "--output" if "--output" in arguments else "-o"
output_path = Path(arguments[arguments.index(output_option) + 1])
Path(os.environ["FAKE_PROMPTFOO_LOG"]).write_text(
    json.dumps({
        "arguments": arguments,
        "output_path": str(output_path),
        "telemetry": os.environ.get("PROMPTFOO_DISABLE_TELEMETRY"),
        "pythonpath": os.environ.get("PYTHONPATH"),
    }),
    encoding="utf-8",
)
exit_code = int(os.environ.get("FAKE_PROMPTFOO_EXIT", "0"))
if exit_code == 0:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(os.environ["FAKE_PROMPTFOO_PAYLOAD"], output_path)
raise SystemExit(exit_code)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    payload = _fake_promptfoo_payload(mode)
    payload_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False) + "\n"
            for record in payload["results"]["results"]
        ),
        encoding="utf-8",
    )
    return executable


def _run_wrapper(
    tmp_path: Path,
    *,
    mode: str = "conformance",
    promptfoo_exit: int = 0,
    keep_artifacts: bool = False,
    malformed_results: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload_path = tmp_path / "payload.json"
    _write_fake_promptfoo(bin_dir, payload_path, mode)
    if malformed_results:
        payload_path.write_text("not json", encoding="utf-8")
    log_path = tmp_path / "promptfoo-log.json"
    env = os.environ.copy()
    env.update(
        {
            "PATH": os.pathsep.join((str(bin_dir), env["PATH"])),
            "FAKE_PROMPTFOO_LOG": str(log_path),
            "FAKE_PROMPTFOO_PAYLOAD": str(payload_path),
            "FAKE_PROMPTFOO_EXIT": str(promptfoo_exit),
            "PYTHONPATH": "/caller/import/root",
        }
    )
    if keep_artifacts:
        env["PRIVACY_EVAL_KEEP_ARTIFACTS"] = "1"
    else:
        env.pop("PRIVACY_EVAL_KEEP_ARTIFACTS", None)

    result = subprocess.run(
        [
            sys.executable,
            str(WRAPPER_PATH),
            mode,
            "--filter-first-n",
            "1",
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert log_path.is_file(), result.stdout + result.stderr
    invocation = json.loads(log_path.read_text(encoding="utf-8"))
    return result, invocation


def test_wrapper_uses_private_temporary_output_and_cleans_it_after_success(
    tmp_path: Path,
) -> None:
    result, invocation = _run_wrapper(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    arguments = invocation["arguments"]
    assert "--no-write" in arguments
    filter_index = arguments.index("--filter-first-n")
    assert arguments[filter_index + 1] == "1"
    assert "--share" not in arguments
    assert "--no-share" not in arguments
    assert invocation["telemetry"] == "1"
    assert invocation["pythonpath"] == os.pathsep.join(
        (str(REPOSITORY_ROOT / "backend"), "/caller/import/root")
    )
    output_path = Path(str(invocation["output_path"]))
    assert not output_path.is_relative_to(REPOSITORY_ROOT)
    assert not output_path.parent.exists()


def test_wrapper_cleans_temporary_output_when_promptfoo_fails(tmp_path: Path) -> None:
    result, invocation = _run_wrapper(tmp_path, promptfoo_exit=7)

    assert result.returncode == 7
    assert not Path(str(invocation["output_path"])).parent.exists()


def test_wrapper_cleans_temporary_output_when_gate_rejects_results(
    tmp_path: Path,
) -> None:
    result, invocation = _run_wrapper(tmp_path, malformed_results=True)

    assert result.returncode != 0
    assert not Path(str(invocation["output_path"])).parent.exists()


def test_wrapper_keeps_temporary_output_only_after_explicit_opt_in(
    tmp_path: Path,
) -> None:
    result, invocation = _run_wrapper(tmp_path, keep_artifacts=True)
    output_path = Path(str(invocation["output_path"]))
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        assert output_path.is_file()
        assert str(output_path.parent) in result.stdout
    finally:
        if output_path.parent.exists():
            shutil.rmtree(output_path.parent)


def test_prompt_lab_wrapper_reports_threshold_breaches_without_failing(
    tmp_path: Path,
) -> None:
    result, _ = _run_wrapper(tmp_path, mode="prompt-lab")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PROMPT_LAB: cases=1" in result.stdout


def test_wrapper_rejects_cache_options() -> None:
    for option in ("--cache", "--no-cache"):
        result = subprocess.run(
            [sys.executable, str(WRAPPER_PATH), "conformance", option],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 2
        assert f"{option} is controlled" in result.stderr


def test_wrapper_reports_missing_promptfoo_cli(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PATH"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, str(WRAPPER_PATH), "conformance"],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 127
    assert "Promptfoo CLIが見つかりません" in result.stderr
