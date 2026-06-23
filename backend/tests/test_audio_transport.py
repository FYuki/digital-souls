import inspect

import pytest


class TestAudioTransport:
    def test_audio_transport_is_abstract(self):
        from app.audio.transport import AudioTransport

        with pytest.raises(TypeError):
            AudioTransport()

    def test_receive_audio_contract_returns_bytes_annotation(self):
        from app.audio.transport import AudioTransport

        signature = inspect.signature(AudioTransport.receive_audio)

        assert signature.return_annotation is bytes

    def test_send_audio_contract_accepts_audio_bytes_and_returns_none(self):
        from app.audio.transport import AudioTransport

        signature = inspect.signature(AudioTransport.send_audio)

        assert signature.parameters["audio"].annotation is bytes
        assert signature.return_annotation is None

    def test_concrete_transport_can_implement_audio_contract(self):
        from app.audio.transport import AudioTransport

        class InMemoryAudioTransport(AudioTransport):
            def __init__(self):
                self.sent_audio = b""

            def receive_audio(self) -> bytes:
                return b"input"

            def send_audio(self, audio: bytes) -> None:
                self.sent_audio = audio

        transport = InMemoryAudioTransport()
        transport.send_audio(b"output")

        assert transport.receive_audio() == b"input"
        assert transport.sent_audio == b"output"
