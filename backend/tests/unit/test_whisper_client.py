import importlib
import io
import logging
import sys
import threading
import time
import types
import wave
from pathlib import Path

import pytest


class _Segment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeWhisperModel:
    instances = []
    creation_delay = 0.0
    transcribe_delay = 0.0
    cuda_available = False

    def __init__(self, *args, **kwargs) -> None:
        if _FakeWhisperModel.creation_delay:
            time.sleep(_FakeWhisperModel.creation_delay)
        self.init_args = args
        self.init_kwargs = kwargs
        self.selected_device = kwargs.get(
            "device", "cuda" if _FakeWhisperModel.cuda_available else "cpu"
        )
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
    _FakeWhisperModel.cuda_available = False
    return importlib.import_module("app.stt.whisper_client")


def _transcriber(client):
    return client.WhisperTranscriber(
        model_name="medium",
        download_root=Path(__file__).parents[3] / ".cache" / "huggingface" / "hub",
    )


class TestWhisperClientTranscribe:
    def test_should_create_model_with_cpu_and_int8_when_transcribing(
        self, monkeypatch
    ) -> None:
        client = _import_client_with_fake_whisper(monkeypatch)
        transcriber = _transcriber(client)

        transcriber.transcribe(b"\x01\x00\x02\x00")

        model = _FakeWhisperModel.instances[0]
        assert model.init_kwargs["device"] == "cpu"
        assert model.init_kwargs["compute_type"] == "int8"

    def test_should_keep_cpu_when_cuda_is_available(self, monkeypatch) -> None:
        client = _import_client_with_fake_whisper(monkeypatch)
        _FakeWhisperModel.cuda_available = True
        transcriber = _transcriber(client)

        transcriber.transcribe(b"\x01\x00\x02\x00")

        assert _FakeWhisperModel.instances[0].selected_device == "cpu"

    def test_should_expose_resolved_device_and_compute_type(self, monkeypatch) -> None:
        client = _import_client_with_fake_whisper(monkeypatch)

        transcriber = _transcriber(client)

        assert transcriber.device == "cpu"
        assert transcriber.compute_type == "int8"

    def test_should_log_resolved_device_and_compute_type_on_initialization(
        self, monkeypatch, caplog
    ) -> None:
        client = _import_client_with_fake_whisper(monkeypatch)

        with caplog.at_level(logging.INFO, logger=client.__name__):
            _transcriber(client)

        assert any(
            "cpu" in record.getMessage() and "int8" in record.getMessage()
            for record in caplog.records
            if record.name == client.__name__
        )

    def test_uses_injected_model_and_download_root(self, monkeypatch, tmp_path):
        client = _import_client_with_fake_whisper(monkeypatch)
        transcriber = client.WhisperTranscriber(
            model_name="large-v3",
            download_root=tmp_path,
        )

        result = transcriber.transcribe(b"\x01\x00\x02\x00")

        assert result == "こんにちは 光織です"
        model = _FakeWhisperModel.instances[0]
        assert model.init_args[0] == "large-v3"
        assert model.init_kwargs["download_root"] == str(tmp_path)

    def test_uses_medium_model_once_for_repeated_transcription(self, monkeypatch):
        client = _import_client_with_fake_whisper(monkeypatch)
        transcriber = _transcriber(client)

        first_result = transcriber.transcribe(b"\x01\x00\x02\x00")
        second_result = transcriber.transcribe(b"\x03\x00\x04\x00")

        assert first_result == "こんにちは 光織です"
        assert second_result == "こんにちは 光織です"
        assert len(_FakeWhisperModel.instances) == 1
        assert _FakeWhisperModel.instances[0].init_args[0] == "medium"
        assert _FakeWhisperModel.instances[0].init_kwargs["download_root"] == str(
            Path(__file__).parent.parent.parent.parent / ".cache" / "huggingface" / "hub"
        )

    def test_passes_audio_bytes_as_file_like_object_and_language_ja(self, monkeypatch):
        client = _import_client_with_fake_whisper(monkeypatch)
        transcriber = _transcriber(client)
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
        transcriber = _transcriber(client)
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
        transcriber = _transcriber(client)
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


class TestIsolatedWhisperTranscriber:
    def _isolated(self, monkeypatch, *, lock_timeout=0.1, inference_timeout=0.1):
        _import_client_with_fake_whisper(monkeypatch)
        sys.modules.pop("app.stt.whisper_isolation", None)
        isolation = importlib.import_module("app.stt.whisper_isolation")
        return isolation, isolation.IsolatedWhisperTranscriber(
            model_name="medium",
            download_root=Path(__file__).parents[3] / ".cache" / "huggingface" / "hub",
            lock_timeout_seconds=lock_timeout,
            inference_timeout_seconds=inference_timeout,
            process_start_method="fork",
        )

    def test_inference_timeout_discards_worker_and_next_request_succeeds(
        self, monkeypatch
    ) -> None:
        isolation, transcriber = self._isolated(
            monkeypatch, inference_timeout=0.03
        )
        try:
            _FakeWhisperModel.transcribe_delay = 0.2
            with pytest.raises(isolation.WhisperTimeoutError) as caught:
                transcriber.transcribe(b"\x01\x00\x02\x00")
            assert caught.value.error_code == "stt_inference_timeout"

            _FakeWhisperModel.transcribe_delay = 0
            assert transcriber.transcribe(b"\x03\x00\x04\x00") == "こんにちは 光織です"
        finally:
            transcriber.close()

    def test_consecutive_timeouts_each_discard_worker_before_recovery(
        self, monkeypatch, caplog
    ) -> None:
        isolation, transcriber = self._isolated(
            monkeypatch, inference_timeout=0.03
        )
        try:
            _FakeWhisperModel.transcribe_delay = 0.2
            with caplog.at_level(logging.WARNING, logger=isolation.__name__):
                for audio in (b"\x01\x00", b"\x02\x00"):
                    with pytest.raises(isolation.WhisperTimeoutError):
                        transcriber.transcribe(audio)

            timeout_logs = [
                record.getMessage()
                for record in caplog.records
                if "reason=inference_timeout" in record.getMessage()
            ]
            assert len(timeout_logs) == 2
            assert all("\\x01" not in message for message in timeout_logs)

            _FakeWhisperModel.transcribe_delay = 0
            assert transcriber.transcribe(b"\x03\x00") == "こんにちは 光織です"
        finally:
            transcriber.close()

    def test_worker_recreation_failure_is_logged_and_later_request_recovers(
        self, monkeypatch, caplog
    ) -> None:
        isolation, transcriber = self._isolated(monkeypatch)
        original_process = transcriber._context.Process

        def fail_process_creation(*args, **kwargs):
            del args, kwargs
            raise OSError("test-only process creation failure")

        try:
            monkeypatch.setattr(transcriber._context, "Process", fail_process_creation)
            with caplog.at_level(logging.WARNING, logger=isolation.__name__):
                with pytest.raises(isolation.WhisperIsolationError) as caught:
                    transcriber.transcribe(b"\x01\x00")
            assert caught.value.error_code == "stt_worker_failed"
            assert any(
                "reason=worker_start_failed" in record.getMessage()
                and "error_type=OSError" in record.getMessage()
                and "test-only" not in record.getMessage()
                for record in caplog.records
            )

            monkeypatch.setattr(transcriber._context, "Process", original_process)
            assert transcriber.transcribe(b"\x03\x00") == "こんにちは 光織です"
        finally:
            transcriber.close()

    def test_lock_wait_timeout_discards_owner_and_next_request_succeeds(
        self, monkeypatch
    ) -> None:
        isolation, transcriber = self._isolated(
            monkeypatch, lock_timeout=0.02, inference_timeout=1
        )
        _FakeWhisperModel.transcribe_delay = 0.15
        first_errors: list[Exception] = []

        def first_request() -> None:
            try:
                transcriber.transcribe(b"\x01\x00")
            except Exception as error:
                first_errors.append(error)

        first = threading.Thread(target=first_request)
        try:
            first.start()
            time.sleep(0.03)
            with pytest.raises(isolation.WhisperTimeoutError) as caught:
                transcriber.transcribe(b"\x02\x00")
            assert caught.value.error_code == "stt_lock_wait_timeout"
            first.join(timeout=1)
            assert len(first_errors) == 1
            assert isinstance(first_errors[0], isolation.WhisperIsolationError)

            _FakeWhisperModel.transcribe_delay = 0
            assert transcriber.transcribe(b"\x03\x00") == "こんにちは 光織です"
        finally:
            transcriber.close()
