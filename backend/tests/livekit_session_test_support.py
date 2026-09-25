"""session資源ownerとruntime managerの単体試験用shell。

#484で`production.py`のsession所有構造は`session_runtime.py`の
`ProductionSessionOwner`へ集約される。テストは実装のコンストラクタに
依存せず、ownerが保持する資源フィールドだけを明示して組み立てる。
"""

from __future__ import annotations

import asyncio
import importlib


def _session_runtime_module():
    return importlib.import_module("app.livekit_transport.session_runtime")


def session_owner(session_id: str = "session", **fields):
    """資源フィールドだけを明示した`ProductionSessionOwner` shell。

    認可・generationの正本は`coordinator`、入力grantは`bridge`側に残る。
    ownerは状態を複製せず、Room・task・reader・cleanup checkpointの
    所有点としてだけ使う。
    """
    session_runtime = _session_runtime_module()
    owner = object.__new__(session_runtime.ProductionSessionOwner)
    owner.session_id = session_id
    owner.room = fields.get("room")
    owner.coordinator = fields.get("coordinator")
    owner.ready = fields.get("ready", asyncio.Event())
    owner.tasks = fields.get("tasks", set())
    owner.participant_tail = fields.get("participant_tail")
    owner.audio_source = fields.get("audio_source")
    owner.core_session = fields.get("core_session")
    owner.bridge = fields.get("bridge")
    owner.screen_client_session_id = fields.get("screen_client_session_id")
    owner.publish_data = fields.get("publish_data")
    owner.readers = fields.get("readers")
    owner.cleanup = fields.get("cleanup")
    return owner


def runtime_shell(**fields):
    """session IDからownerへの対応だけを持つ`ProductionRuntimeManager` shell。"""
    session_runtime = _session_runtime_module()
    runtime = object.__new__(session_runtime.ProductionRuntimeManager)
    runtime._owners = fields.get("owners", {})
    return runtime
