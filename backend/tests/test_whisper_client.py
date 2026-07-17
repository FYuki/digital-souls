import importlib
import io
import sys
import threading
import time
import types
import wave
from pathlib import Path


class _Segment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeWhisperModel:
    instances = []
    creation_delay = 0.0
    transcribe_delay = 0.0

    def __init__(self, *args, **kwargs) -> None:
        if _FakeWhisperModel.creation_delay:
            time.sleep(_FakeWhisperModel.creation_delay)
        self.init_args = args
        self.init_kwargs = kwargs
        self.transcribe_calls = []
        self.concurrent_transcribe_count = 0
        self.max_concurrent_transcribe_count = 0
        self._concurrency_lock = threading.Lock()
        _FakeWhisperModel.instances.append(self)

    def transcribe(self, audio_source, **kwargs):
        with self._concurrency_lock:
            self.concurrent_transcribe_count += 1
            self.max_concurrent_transcribe_count = max(
                self.max_concurrent_transcribe_count,
                self.concurrent_transcribe_count,
            )
        try:
            if _FakeWhisperModel.transcribe_delay:
                time.sleep(_FakeWhisperModel.transcribe_delay)
            self.transcribe_calls.append((audio_source, kwargs))
            return self._iter_segments(), object()
        finally:
            with self._concurrency_lock:
                self.concurrent_transcribe_count -= 1

    def _iter_segments(self):
        yield _Segment("こんにちは")
        yield _Segment(" 光織です")


def _import_client_with_fake_whisper(monkeypatch):
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    sys.modules.pop("app.stt.whisper_client", None)
    _FakeWhisperModel.instances.clear()
    _FakeWhisperModel.creation_delay = 0.0
    _FakeWhisperModel.transcribe_delay = 0.0
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
        assert _FakeWhisperModel.instances[0].init_kwargs["download_root"] == str(
            Path(__file__).parent.parent.parent / ".cache" / "huggingface" / "hub"
        )

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

    def test_serializes_concurrent_transcribe_calls(self, monkeypatch):
        client = _import_client_with_fake_whisper(monkeypatch)
        _FakeWhisperModel.transcribe_delay = 0.05
        transcriber = client.WhisperTranscriber()
        transcriber.transcribe(b"\x01\x00\x02\x00")
        model = _FakeWhisperModel.instances[0]
        results = []
        errors = []
        start = threading.Barrier(4)

        def transcribe_audio(audio: bytes) -> None:
            try:
                start.wait()
                results.append(transcriber.transcribe(audio))
            except Exception as exc:  # pragma: no cover - surfaced by assertion below
                errors.append(exc)

        threads = [
            threading.Thread(target=transcribe_audio, args=(b"\x03\x00\x04\x00",)),
            threading.Thread(target=transcribe_audio, args=(b"\x05\x00\x06\x00",)),
            threading.Thread(target=transcribe_audio, args=(b"\x07\x00\x08\x00",)),
        ]

        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join()

        assert errors == []
        assert results == [
            "こんにちは 光織です",
            "こんにちは 光織です",
            "こんにちは 光織です",
        ]
        assert model.max_concurrent_transcribe_count == 1
