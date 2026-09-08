from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from livekit.rtc._proto import stats_pb2 as proto
from livekit.rtc.room import RtcStats

from app.livekit_transport.rtc_diagnostic import RtcIngressDiagnostic, summarize_rtc_stats


def fixture() -> RtcStats:
    return RtcStats(publisher_stats=[proto.RtcStats(transport=proto.RtcStats.Transport(
        rtc=proto.RtcStatsData(id="private-transport-id", timestamp=123456),
        transport=proto.TransportStats(bytes_sent=12, dtls_state=2,
            ice_local_username_fragment="private-ice-credential", selected_candidate_pair_id="private-address")))],
        subscriber_stats=[proto.RtcStats(data_channel=proto.RtcStats.DataChannel(
            rtc=proto.RtcStatsData(id="private-channel-id"), dc=proto.DataChannelStats(
                label="private-label", protocol="private-protocol", state=1, messages_received=3)))])


def test_stats_preserve_missing_fields_without_copying_identifiers():
    rows = summarize_rtc_stats(fixture())
    assert rows == [dict(side="publisher", kind="transport", dtls_state=2, ice_state=None,
        packets_sent=None, packets_received=None, bytes_sent=12, bytes_received=None),
        dict(side="subscriber", kind="data_channel", channel="other", state=1,
            messages_sent=None, messages_received=3, bytes_sent=None, bytes_received=None)]
    assert "private" not in json.dumps(rows)


def test_oversized_stats_are_not_silently_truncated():
    stats = fixture(); stats.publisher_stats *= 17
    with pytest.raises(ValueError, match="limit"):
        summarize_rtc_stats(stats)


def test_ingress_is_unarmed_before_reconnect_and_bounded_without_payload(caplog):
    observer = RtcIngressDiagnostic(lambda: 2)
    observer.ingress("private", "current")
    assert not caplog.records
    observer.armed = True
    observer.ingress("secret-topic", "secret-participant")
    for _ in range(520): observer.ingress("private", "current")
    assert len(caplog.records) == 513
    assert "secret" not in caplog.text
    assert "topic=other participant=other" in caplog.records[0].message
    assert "topic=overflow" in caplog.records[-1].message


def test_cancel_during_stats_request_closes_observer_and_does_not_swallow_cancel(caplog):
    async def exercise():
        entered = asyncio.Event(); cancelled = asyncio.Event()
        async def stats():
            entered.set()
            try: await asyncio.Future()
            finally: cancelled.set()
        observer = RtcIngressDiagnostic(lambda: 1)
        task = asyncio.create_task(observer.sample(SimpleNamespace(get_rtc_stats=stats)))
        await entered.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert cancelled.is_set()
    asyncio.run(exercise())
    assert len(caplog.records) == 1 and '"status": "closed"' in caplog.text


@pytest.mark.parametrize("mode", ["success", "failure", "generation_changed"])
def test_sampling_rejects_exception_text_and_cross_generation_snapshot(mode, monkeypatch, caplog):
    async def exercise():
        generation = 1
        async def stats():
            nonlocal generation
            if mode == "failure": raise RuntimeError("private-error-credential")
            if mode == "generation_changed": generation = 2
            return fixture()
        async def stop(_delay): raise asyncio.CancelledError()
        monkeypatch.setattr(asyncio, "sleep", stop)
        observer = RtcIngressDiagnostic(lambda: generation)
        with pytest.raises(asyncio.CancelledError):
            await observer.sample(SimpleNamespace(get_rtc_stats=stats))
    asyncio.run(exercise())
    rows = [json.loads(record.message.removeprefix("RTC stats: ")) for record in caplog.records]
    assert rows[-1] == {"status": "closed"}
    assert rows[0]["status"] == {"success": "captured", "failure": "unavailable", "generation_changed": "generation_changed"}[mode]
    if mode != "success": assert rows[0]["rows"] == []
    assert "private" not in caplog.text
