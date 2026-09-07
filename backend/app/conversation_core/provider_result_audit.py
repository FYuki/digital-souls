"""Coreがprovider iteratorから受け取った結果の数値だけを残す。"""
from __future__ import annotations

PROVIDER_RESULT_METRICS = frozenset({
    'provider_result_text_after_cancel_events', 'provider_result_text_after_cancel_utf16_units',
    'provider_result_audio_after_cancel_events', 'provider_result_audio_after_cancel_bytes',
    'provider_result_observation_closed', 'provider_result_observation_valid',
})


class ProviderResultAudit:
    """生成時刻ではなくCoreの受領境界。重複yieldも受領件数から消さない。"""

    def __init__(self) -> None:
        self.text_events = 0
        self.text_units = 0
        self.audio_events = 0
        self.audio_bytes = 0
        self.valid = True

    def text(self, value: str, *, cancelled: bool) -> None:
        if not cancelled:
            return
        if not isinstance(value, str):
            self.valid = False
            return
        self.text_events += 1
        self.text_units += len(value.encode('utf-16-le', errors='surrogatepass')) // 2

    def audio(self, value: bytes, *, cancelled: bool) -> None:
        if not cancelled:
            return
        if not isinstance(value, bytes):
            self.valid = False
            return
        self.audio_events += 1
        self.audio_bytes += len(value)

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
