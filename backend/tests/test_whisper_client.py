import importlib
import io
import sys
import threading
import time
import types
import wave


class _Segment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeWhisperModel:
    instances = []
    creation_delay = 0.0

    def __init__(self, *args, **kwargs) -> None:
        if _FakeWhisperModel.creation_delay:
            time.sleep(_FakeWhisperModel.creation_delay)
        self.init_args = args
        self.init_kwargs = kwargs
        self.transcribe_calls = []
        _FakeWhisperModel.instances.append(self)

    def transcribe(self, audio_source, **kwargs):
        self.transcribe_calls.append((audio_source, kwargs))
        return [_Segment("こんにちは"), _Segment(" 光織です")], object()


def _import_client_with_fake_whisper(monkeypatch):
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    sys.modules.pop("app.stt.whisper_client", None)
    _FakeWhisperModel.instances.clear()
    _FakeWhisperModel.creation_delay = 0.0
    return importlib.import_module("app.stt.whisper_client")


class TestWhisperClientTranscribe:
    def test_uses_medium_model_once_for_repeated_transcription(self, monkeypatch):
        client = _import_client_with_fake_whisper(monkeypatch)
        transcriber = client.WhisperTranscriber()

        first_result = transcriber.transcribe(b"\x01\x00\x02\x00")
        second_result = transcriber.transcribe(b"\x03\x00\x04\x00")

        assert first_result == "こんにちは 光織です"
        assert second_result == "こんにちは 光織です"
        assert len(_FakeWhisperModel.instances) == 1
        assert _FakeWhisperModel.instances[0].init_args[0] == "medium"

    def test_passes_audio_bytes_as_file_like_object_and_language_ja(self, monkeypatch):
        client = _import_client_with_fake_whisper(monkeypatch)
        transcriber = client.WhisperTranscriber()
        pcm_audio = b"\x01\x00\x02\x00"

        transcriber.transcribe(pcm_audio)

        model = _FakeWhisperModel.instances[0]
        audio_source, kwargs = model.transcribe_calls[0]
        assert isinstance(audio_source, io.BytesIO)
        assert audio_source.getvalue().startswith(b"RIFF")
        assert kwargs["language"] == "ja"

        audio_source.seek(0)
        with wave.open(audio_source, "rb") as wav_file:
            assert wav_file.getnchannels() == 1
            assert wav_file.getsampwidth() == 2
            assert wav_file.getframerate() == 16000
            assert wav_file.readframes(2) == pcm_audio

    def test_creates_one_model_for_concurrent_first_transcriptions(self, monkeypatch):
        client = _import_client_with_fake_whisper(monkeypatch)
        _FakeWhisperModel.creation_delay = 0.05
        transcriber = client.WhisperTranscriber()
        results = []
        start = threading.Barrier(3)

        def transcribe_audio(audio: bytes) -> None:
            start.wait()
            results.append(transcriber.transcribe(audio))

        threads = [
            threading.Thread(target=transcribe_audio, args=(b"\x01\x00\x02\x00",)),
            threading.Thread(target=transcribe_audio, args=(b"\x03\x00\x04\x00",)),
        ]

        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join()

        assert results == ["こんにちは 光織です", "こんにちは 光織です"]
        assert len(_FakeWhisperModel.instances) == 1
