from __future__ import annotations


class RoomCleanupPendingError(RuntimeError):
    """LiveKit Roomの削除だけが再試行待ちであることを表す。"""
