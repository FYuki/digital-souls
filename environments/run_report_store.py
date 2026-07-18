from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

from run_report_validation import RunReportError, validate_run_report


class RunReportStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f"{path.name}.lock")

    def save(self, report: dict[str, object]) -> None:
        with self._exclusive_lock():
            self._save_unlocked(report)

    def update(
        self, transform: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        with self._exclusive_lock():
            updated = transform(self.load())
            self._save_unlocked(updated)
            return updated

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lock:
            flock(lock.fileno(), LOCK_EX)
            try:
                yield
            finally:
                flock(lock.fileno(), LOCK_UN)

    def _save_unlocked(self, report: dict[str, object]) -> None:
        validate_run_report(report)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                json.dump(report, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def load(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RunReportError(f"run report cannot be loaded: {error}") from error
        if not isinstance(value, dict):
            raise RunReportError("run report must be an object")
        validate_run_report(value)
        return value
