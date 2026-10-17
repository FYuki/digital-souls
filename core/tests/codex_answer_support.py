"""Synthetic executable only; never use the operator's Codex home."""

import json
import sys
from pathlib import Path
from typing import Any

from .test_semantic_answer_external import external_data

FLAGS = (
    "--ephemeral --ignore-rules --ignore-user-config --json --sandbox "
    "--skip-git-repo-check --strict-config --model --color --cd --config --disable"
)


def fake_codex(tmp_path: Path, *, fault: str = "") -> tuple[dict[str, Any], Path]:
    executable = tmp_path / "codex"
    record = tmp_path / "calls.jsonl"
    home = tmp_path / "fake-home"
    home.mkdir()
    (home / "auth.json").write_text("credential-file-sentinel")
    executable.write_text(
        f"""#!{sys.executable}
import json, os, pathlib, sys, time
args = sys.argv[1:]
with pathlib.Path({str(record)!r}).open("a") as out:
    data = {{"argv":args,"env":dict(os.environ),"cwd":os.getcwd(),
            "empty":not list(pathlib.Path.cwd().iterdir()),
            "stdin":sys.stdin.read() if "--json" in args else None}}
    out.write(json.dumps(data) + "\\n")
fault = {fault!r}
print("stderr-sentinel", file=sys.stderr)
if args == ["--version"]:
    versions = {{"old":"codex-cli 0.151.9","version":"version-sentinel"}}
    print(versions.get(fault, "codex-cli 0.162.1"))
elif args == ["exec", "--help"]:
    print({FLAGS!r}.replace("--ignore-rules", "") if fault == "flag" else {FLAGS!r})
elif "--json" in args:
    if fault == "timeout": time.sleep(5)
    if fault == "exit":
        print("stdout-sentinel")
        sys.exit(7)
    if fault == "json": print("stdout-sentinel")
    elif fault == "missing": print(json.dumps({{"type":"turn.completed"}}))
    elif fault == "tool":
        print(json.dumps({{"type":"item.completed","item":{{"type":"mcp_tool_call"}}}}))
    elif fault == "failed":
        print(json.dumps({{"type":"turn.failed","error":{{"message":"stdout-sentinel"}}}}))
    else:
        print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":"intermediate"}}}}))
        item = {{"type":"agent_message","text":"Synthetic final answer"}}
        print(json.dumps({{"type":"item.completed","item":item}}))
        print(json.dumps({{"type":"turn.completed"}}))
else: sys.exit(8)
"""
    )
    executable.chmod(0o700)
    data = external_data()
    data.pop("api_key_env")
    data.pop("api_base_env")
    chat = data["chat"]
    assert isinstance(chat, dict)
    chat["model"] = "openai/gpt-6-luna"
    data.update(
        answer_provider="codex_subscription", codex_executable=str(executable), codex_home=str(home)
    )
    return data, record


def calls(record: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
