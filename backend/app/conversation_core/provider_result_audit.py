"""Coreがprovider iteratorから受け取った結果の数値だけを残す。"""
from __future__ import annotations

PROVIDER_RESULT_METRICS = frozenset({
    'provider_result_text_after_cancel_events', 'provider_result_text_after_cancel_utf16_units',
    'provider_result_audio_after_cancel_events', 'provider_result_audio_after_cancel_bytes',
    'provider_result_observation_closed', 'provider_result_observation_valid',
})


PROVIDER_STOPPING_METRICS = frozenset({
    'provider_result_text_during_stop_events', 'provider_result_text_during_stop_utf16_units',
    'provider_result_audio_during_stop_events', 'provider_result_audio_during_stop_bytes',
    'provider_result_during_stop_valid',
})


class ProviderResultAudit:
    """生成時刻ではなくCoreの受領境界。重複yieldも受領件数から消さない。"""

    def __init__(self) -> None:
        self.text_events = 0
        self.text_units = 0
        self.audio_events = 0
        self.audio_bytes = 0
        self.valid = True
        self.stopping_seen = False
        self.stopping_valid = True
        self.stopping_text_events = 0
        self.stopping_text_units = 0
        self.stopping_audio_events = 0
        self.stopping_audio_bytes = 0

    def text(self, value: str, *, cancelled: bool, stopping: bool = False) -> None:
        if stopping:
            self.stopping_seen = True
            if not isinstance(value, str):
                self.stopping_valid = False
            else:
                self.stopping_text_events += 1
                self.stopping_text_units += len(value.encode('utf-16-le', errors='surrogatepass')) // 2
        if not cancelled:
            return
        if not isinstance(value, str):
            self.valid = False
            return
        self.text_events += 1
        self.text_units += len(value.encode('utf-16-le', errors='surrogatepass')) // 2

    def audio(self, value: bytes, *, cancelled: bool, stopping: bool = False) -> None:
        if stopping:
            self.stopping_seen = True
            if not isinstance(value, bytes):
                self.stopping_valid = False
            else:
                self.stopping_audio_events += 1
                self.stopping_audio_bytes += len(value)
        if not cancelled:
            return
        if not isinstance(value, bytes):
            self.valid = False
            return
        self.audio_events += 1
        self.audio_bytes += len(value)

    def stopping_statistics(self) -> dict[str, int]:
        # 停止中の受領を取消成立後のゼロへ紛れ込ませず、別の数値として残す。
        if not self.stopping_seen:
            return {}
        return {
            'provider_result_text_during_stop_events': self.stopping_text_events,
            'provider_result_text_during_stop_utf16_units': self.stopping_text_units,
            'provider_result_audio_during_stop_events': self.stopping_audio_events,
            'provider_result_audio_during_stop_bytes': self.stopping_audio_bytes,
            'provider_result_during_stop_valid': int(self.stopping_valid),
        }

    def closed_statistics(self) -> dict[str, int]:
        # 呼出元はLLM/TTS consumer両方の終了を待ってからだけ、このsnapshotを公開する。
        return {
            'provider_result_text_after_cancel_events': self.text_events,
            'provider_result_text_after_cancel_utf16_units': self.text_units,
            'provider_result_audio_after_cancel_events': self.audio_events,
            'provider_result_audio_after_cancel_bytes': self.audio_bytes,
            'provider_result_observation_closed': 1,
            'provider_result_observation_valid': int(self.valid),
        }
