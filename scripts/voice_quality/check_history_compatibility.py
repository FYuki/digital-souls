"""新規の検証用SQLiteだけで、移設前後の履歴Repositoryを往復する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORAGE = (
    "backend/app/conversation_history",
    "backend/app/runtime_data_root.py",
    "backend/app/runtime_paths.py",
)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def check(before: Path, application_commit: str) -> dict:
    if sys.flags.optimize:
        raise ValueError("検証assertを無効化する最適化実行は使用できません")
    before = before.resolve(strict=True)
    source_commit = git(before, "rev-parse", application_commit + "^{commit}")
    after_commit = git(ROOT, "rev-parse", "HEAD")
    before_commit = git(before, "rev-parse", "HEAD")
    # 今回は保存形式を変更しない契約。異なる保存実装を同一とみなさない。
    for repo in (before, ROOT):
        if git(repo, "diff", source_commit, "--", *STORAGE) or git(
            repo, "ls-files", "--others", "--exclude-standard", "--", *STORAGE
        ):
            raise ValueError("履歴保存コードが移設前の基準と異なります")
    worker = Path(__file__).with_name("history_compatibility_worker.py")
    rows = []
    with tempfile.TemporaryDirectory(prefix="ds358-history-roundtrip-") as temp:
        database = Path(temp) / "history.db"
        for phase, repo in (("before", before), ("after", ROOT),
                            ("rollback", before), ("verify", ROOT)):
            env = dict(os.environ, PYTHONPATH=str(repo / "backend"), PYTHON_DOTENV_DISABLED="1")
            result = subprocess.run(
                [sys.executable, str(worker), str(database), phase, str(repo)],
                cwd=repo, env=env, capture_output=True, text=True, check=True,
            )
            row = json.loads(result.stdout)
            if rows and row["pre_rows_sha256"] != rows[-1]["post_rows_sha256"]:
                raise ValueError("版の切替で既存の履歴行が変化しました")
            rows.append(row)
    return {
        "scope": "repository_history_cross_revision_roundtrip",
        "before_application_commit": source_commit,
        "before_worktree_commit": before_commit,
        "after_commit": after_commit,
        "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
        "runs": rows,
        "new_temporary_database_only": True,
        "existing_runtime_data_accessed": False,
        "application_history_storage_diff": False,
        "passed": True,
        "limits": [
            "実SQLiteとRepositoryの互換性確認であり、稼働中Sessionの移行ではない",
            "FE/BE一括配備・旧client拒否・実マイク受入は別途必要",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-repo", type=Path, required=True)
    parser.add_argument("--before-application-commit", default="07b8b1ee638ca222afe61983ebaf17c6edaaf1f7")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("既存の証跡を上書きしません")
    report = check(args.before_repo, args.before_application_commit)
    with args.output.open("x") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "phases": len(report["runs"])}))


if __name__ == "__main__":
    main()
