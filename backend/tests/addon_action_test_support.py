"""検証で共有する最小の実行環境・合成応答。テスト収集に依存しない。"""

from app.addon_action.policy import ActionPolicy
from app.addon_action.store import ActionStore
from app.tool_use.projection import Sanitizer
from app.privacy.contracts import ScanSuccess
import time


class Scanner:
    def scan(self, text):
        return ScanSuccess(findings=())


async def allow(arguments):
    return True


def policy(tmp_path, **kwargs):
    return ActionPolicy(
        ActionStore(tmp_path / "actions.sqlite3", clock=kwargs.get("clock", time.time)),
        Sanitizer(Scanner()),
        egress=allow,
        **kwargs,
    )
