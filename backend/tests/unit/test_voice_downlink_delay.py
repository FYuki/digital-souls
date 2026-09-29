"""下り遅延 d の設定解決を検証する。

対象: Issue #540。`DS_VOICE_DOWNLINK_DELAY_MS` 環境変数から
非負整数として解決し、BE の送出位置推定と出力完了判定で同じ値を使う。
"""
from __future__ import annotations

import asyncio

import pytest

from app.livekit_transport.production import (
    DEFAULT_VOICE_DOWNLINK_DELAY_MS,
    LiveKitConfigurationError,
    resolve_voice_downlink_delay_ms,
)
from app.livekit_transport.response_audio import ResponseAudioTracks


class TestDownlinkDelayConfiguration:
    """下り遅延 d の環境変数解決を検証する。"""

    def test_downlink_delay_from_env_var(self):
        """DS_VOICE_DOWNLINK_DELAY_MS が正しく解決される。"""
        assert resolve_voice_downlink_delay_ms(
            {"DS_VOICE_DOWNLINK_DELAY_MS": "150"}
        ) == 150

    def test_downlink_delay_default_value(self):
        """環境変数未設定時のデフォルト値が使われる。"""
        assert resolve_voice_downlink_delay_ms({}) == DEFAULT_VOICE_DOWNLINK_DELAY_MS

    def test_downlink_delay_rejects_negative(self):
        """負の値は拒否される。"""
        with pytest.raises(LiveKitConfigurationError):
            resolve_voice_downlink_delay_ms({"DS_VOICE_DOWNLINK_DELAY_MS": "-1"})

    def test_downlink_delay_rejects_non_integer(self):
        """整数以外の値は拒否される。"""
        for value in ["abc", "1.5", ""]:
            with pytest.raises(LiveKitConfigurationError):
                resolve_voice_downlink_delay_ms({"DS_VOICE_DOWNLINK_DELAY_MS": value})

    def test_same_delay_used_for_estimation_and_completion(self):
        """再生済み範囲推定と出力完了判定で同じ d が使われる。

        ResponseAudioTracks は1本の downlink_delay_seconds を
        stop_response の推定時刻減算と finish_response の完了待ちの両方に使う。
        """
        tracks = ResponseAudioTracks(
            room=object(),  # 使用しない
            downlink_delay_seconds=0.25,
        )
        # 送出停止の推定時刻減算とfinish待機の両方が同じ属性を参照する。
        assert tracks._downlink_delay_seconds == 0.25

    def test_stop_response_subtracts_delay_from_decision_time(self):
        """decision時刻からdを引いた時点の送出量を推定に使う結線を検証する。"""
        captured: dict[str, int] = {}

        class _Pacer:
            input_sample_count = 4800

            def sent_sample_count(self, at_ns: int) -> int:
                captured["at_ns"] = at_ns
                return 960

            async def aclose(self):
                return None

        class _Source:
            def clear_queue(self):
                return None

        class _Track:
            response_id = "r1"
            segment_sample_ends = [960, 1920]
            pacer = _Pacer()
            source = _Source()
            source_closed = False

            class track:  # noqa: D106 - fake track object
                @staticmethod
                def mute():
                    return None

        tracks = ResponseAudioTracks(
            room=object(),
            downlink_delay_seconds=0.3,
        )
        tracks._current = _Track()

        async def exercise() -> int:
            return await tracks.stop_response("r1", decided_at_ns=1_000_000_000)

        result = asyncio.run(exercise())
        # d=300ms減算後の時刻で送出位置を引く。
        assert captured["at_ns"] == 1_000_000_000 - 300_000_000
        # 960sample送出済み → segment境界で丸めて 1 区間。
        assert result == 1

    def test_stop_response_rounds_down_mid_segment_position(self):
        """区間途中の送出位置は区間境界へ切り下げ、次区間を再生済みに含めない。"""

        class _Pacer:
            input_sample_count = 4800

            def sent_sample_count(self, _at_ns: int) -> int:
                return 1500

            async def aclose(self):
                return None

        class _Source:
            def clear_queue(self):
                return None

        class _Track:
            response_id = "r1"
            segment_sample_ends = [960, 1920]
            pacer = _Pacer()
            source = _Source()
            source_closed = False

            class track:  # noqa: D106 - fake track object
                @staticmethod
                def mute():
                    return None

        tracks = ResponseAudioTracks(
            room=object(),
            downlink_delay_seconds=0.3,
        )
        tracks._current = _Track()

        async def exercise() -> int:
            return await tracks.stop_response("r1", decided_at_ns=1_000_000_000)

        # [960, 1920] の末尾に対して 1500 sample 送出済み → 第2区間は含めず 1。
        assert asyncio.run(exercise()) == 1


class TestDownlinkDelayProductionWiring:
    """P-004: 環境変数で指定した d が本番構築経路を通り、両判定の共通値となる。"""

    def test_env_delay_reaches_stop_and_finish_through_production_wiring(
        self, monkeypatch
    ):
        """DS_VOICE_DOWNLINK_DELAY_MS=150 が production 構築を経て実トラックへ届く。

        設定解決とトラック生成を別々にせず、`configure_production_resources` →
        `ProductionRuntimeManager._prepare_output_track` の本番経路で実トラックを得て、
        停止推定と完了待機が同じ d=150ms で動くことを観測する。
        """
        from unittest.mock import Mock

        from app.livekit_transport.production import configure_production_resources

        monkeypatch.setenv("LIVEKIT_URL", "ws://127.0.0.1:7880")
        monkeypatch.setenv("LIVEKIT_API_KEY", "test-api-key")
        monkeypatch.setenv("LIVEKIT_API_SECRET", "test-api-secret")
        monkeypatch.setenv("DS_VOICE_DOWNLINK_DELAY_MS", "150")

        async def exercise() -> None:
            resources = await configure_production_resources(
                core_session_factory=Mock(),
                screen_session_revoker=Mock(),
                session_trace_recorder=None,
                measurement_kind="automated_test",
                conversations=Mock(),
            )
            assert resources is not None
            try:
                room = Mock()

                class _Pacer:
                    input_sample_count = 4800

                    # d=150msなら評価時点850ms→1920sample、d=300msなら700ms→960sample。
                    def sent_sample_count(self, at_ns: int) -> int:
                        return 1920 if at_ns >= 850_000_000 else 960

                    async def aclose(self) -> None:
                        return None

                    def stop(self) -> None:
                        return None

                class _Source:
                    def clear_queue(self) -> None:
                        return None

                class _Current:
                    response_id = "r1"
                    segment_sample_ends = [960, 1920]
                    pacer = _Pacer()
                    source = _Source()
                    source_closed = False
                    client_ready = asyncio.Event()

                    class track:  # noqa: D106 - fake track object
                        @staticmethod
                        def mute() -> None:
                            return None

                # 停止推定: 本番構築した実トラックがd=150msで評価時点を選ぶ。
                stop_tracks = (
                    await resources.runtime_manager._prepare_output_track(room)
                )
                stop_tracks._current = _Current()
                prefix = await stop_tracks.stop_response(
                    "r1", decided_at_ns=1_000_000_000
                )
                assert prefix == 2

                # 出力完了: 同じ構築経路の実トラックがd=150msだけ待機する。
                sleeps: list[float] = []
                real_sleep = asyncio.sleep

                async def record_sleep(seconds: float) -> None:
                    sleeps.append(seconds)
                    await real_sleep(0)

                finish_tracks = (
                    await resources.runtime_manager._prepare_output_track(room)
                )
                finish_current = _Current()

                async def finished() -> None:
                    return None

                finish_current.pacer.finish = finished
                finish_tracks._current = finish_current

                monkeypatch.setattr(asyncio, "sleep", record_sleep)
                try:
                    await finish_tracks.finish_response("r1")
                finally:
                    monkeypatch.setattr(asyncio, "sleep", real_sleep)
                assert 0.15 in sleeps
                assert 0.3 not in sleeps
            finally:
                await resources.api.aclose()

        asyncio.run(exercise())
