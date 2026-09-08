"""希望設定だけを原子的に保存する。接続設定とhealthは保存しない。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from app.restore_intent import fsync_directory


class SettingsDurabilityError(OSError):
    """置換済みだが、ホスト障害に対する耐久性を確認できない。"""


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._values: dict[str, bool] = {}
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text())
                values = raw["users"]["local"]
                if (
                    raw["version"] != 1
                    or not isinstance(values, dict)
                    or any(
                        not isinstance(key, str) or type(value) is not bool
                        for key, value in values.items()
                    )
                ):
                    raise ValueError()
                self._values = values
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError("invalid addon settings") from None

    def initial(self, connection_id: str, default: bool) -> bool:
        if connection_id not in self._values:
            self.save(connection_id, default)
        return self._values[connection_id]

    def save(self, connection_id: str, enabled: bool) -> None:
        values = {**self._values, connection_id: enabled}
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=self.path.parent, delete=False, encoding="utf-8"
                ) as handle:
                    name = handle.name
                    json.dump({"version": 1, "users": {"local": values}}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, self.path)
                # 置換後は再起動時にも読まれる値へ揃える。同期失敗は未反映と区別する。
                self._values = values
                try:
                    fsync_directory(self.path.parent)
                except OSError:
                    raise SettingsDurabilityError("settings durability uncertain") from None
            finally:
                if name is not None:
                    Path(name).unlink(missing_ok=True)
        self._values = values
