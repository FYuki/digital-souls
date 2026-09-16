"""全スレッドの会話処理を、バックグラウンドの記憶形成より優先する。"""

from datetime import datetime, timedelta
import sqlite3


def conversation_idle(
    history: sqlite3.Connection, *, now: datetime, quiet_seconds: float = 5,
    stale_after: timedelta = timedelta(minutes=5),
) -> bool:
    """別スレッドを含む会話処理を優先し、会話直後の連続入力にも短い猶予を置く。"""
    from app.conversation_history._sqlite import format_datetime, parse_datetime
    if stale_after <= timedelta(0):
        raise ValueError("processing timeout must be positive")
    # 起動時にはfreshだった中断ターンも、UIで履歴を開くまで永久に待たない。
    # 履歴本文・状態は変更せず、会話側と同じstale期限で負荷判定だけを解除する。
    row = history.execute(
        "SELECT SUM(CASE WHEN status='processing' AND updated_at>=? THEN 1 ELSE 0 END),"
        "MAX(updated_at) FROM conversation_turns", (format_datetime(now - stale_after),),
    ).fetchone()
    if row is None:
        return True
    if row[0]:
        return False
    return row[1] is None or (now-parse_datetime(row[1])).total_seconds() >= quiet_seconds
