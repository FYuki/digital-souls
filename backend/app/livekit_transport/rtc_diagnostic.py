"""明示的な障害診断用。RTC統計から固定enum・件数だけを抽出する。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from livekit.rtc.room import RtcStats, Room

logger = logging.getLogger(__name__)


def summarize_rtc_stats(stats: RtcStats) -> list[dict[str, str | int | None]]:
    rows: list[dict[str, str | int | None]] = []
    for side, values in (("publisher", stats.publisher_stats), ("subscriber", stats.subscriber_stats)):
        for record in values:
            kind = record.WhichOneof("stats")
            if kind == "transport":
                data = record.transport.transport
                present = {field.name for field, _ in data.ListFields()}
                row: dict[str, str | int | None] = {"side": side, "kind": "transport"}
                for field in ("dtls_state", "ice_state", "packets_sent", "packets_received", "bytes_sent", "bytes_received"):
                    row[field] = getattr(data, field) if field in present else None
                rows.append(row)
            elif kind == "data_channel":
                channel = record.data_channel.dc
                present = {field.name for field, _ in channel.ListFields()}
                row = {"side": side, "kind": "data_channel", "channel":
                    {"_reliable": "reliable", "_lossy": "lossy"}.get(channel.label, "other")}
                for field in ("state", "messages_sent", "messages_received", "bytes_sent", "bytes_received"):
                    row[field] = getattr(channel, field) if field in present else None
                rows.append(row)
            if len(rows) > 16:
                raise ValueError("RTC diagnostic row limit exceeded")
    return rows


class RtcIngressDiagnostic:
    def __init__(self, generation: Callable[[], int]) -> None:
        self.generation = generation
        self.ingress_count = 0
        self.armed = False

    def ingress(self, topic: str, participant: str) -> None:
        if not self.armed or self.ingress_count > 512:
            return
        topic = topic if topic in {"private", "application", "screen"} else "other"
        participant = participant if participant in {"current", "other", "missing"} else "other"
        if self.ingress_count == 512:
            topic = "overflow"
        self.ingress_count += 1
        logger.warning("RTC ingress: topic=%s participant=%s generation=%d at_ms=%d",
            topic, participant, self.generation(), time.monotonic_ns() // 1_000_000)

    async def sample(self, room: Room) -> None:
        self.armed = True
        try:
            for index in range(150):
                generation = self.generation()
                started = time.monotonic_ns() // 1_000_000
                status = "captured"
                rows: list[dict[str, str | int | None]] = []
                try:
                    async with asyncio.timeout(0.4):
                        stats = await room.get_rtc_stats()
                    if generation != self.generation():
                        status = "generation_changed"
                    else:
                        rows = summarize_rtc_stats(stats)
                except Exception:
                    # 認証先・例外本文・SDP・候補アドレスは記録しない。欠測を明示して観測だけ続ける。
                    status = "unavailable"
                logger.warning("RTC stats: %s", json.dumps({"status": status, "index": index,
                    "generation": generation, "started_at_ms": started,
                    "observed_at_ms": time.monotonic_ns() // 1_000_000, "rows": rows}))
                await asyncio.sleep(0.1)
        finally:
            logger.warning("RTC stats: %s", json.dumps({"status": "closed"}))
