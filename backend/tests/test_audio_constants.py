from app.audio.constants import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
)
from app.routers.ws import MAX_AUDIO_FRAME_BYTES, MAX_AUDIO_FRAME_SECONDS


class TestPcmFormatConstants:
    def test_sample_rate_is_16_khz(self):
        assert PCM_SAMPLE_RATE_HZ == 16_000

    def test_audio_is_mono(self):
        assert PCM_CHANNELS == 1

    def test_sample_width_is_16_bit(self):
        assert PCM_SAMPLE_WIDTH_BYTES == 2


class TestMaximumAudioFrameSize:
    def test_is_calculated_from_shared_pcm_format(self):
        expected_frame_bytes = (
            PCM_SAMPLE_RATE_HZ
            * PCM_CHANNELS
            * PCM_SAMPLE_WIDTH_BYTES
            * MAX_AUDIO_FRAME_SECONDS
        )

        assert MAX_AUDIO_FRAME_BYTES == expected_frame_bytes

    def test_preserves_30_second_pcm_frame_limit(self):
        assert MAX_AUDIO_FRAME_BYTES == 960_000
