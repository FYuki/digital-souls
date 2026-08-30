from __future__ import annotations

import io
import logging
import multiprocessing
import threading
import wave
from collections.abc import Iterable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Protocol, cast

from whisper_service.config import WhisperServiceConfig


logger = logging.getLogger(__name__)
PCM_SAMPLE_RATE_HZ = 16_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH_BYTES = 2
STARTUP_TIMEOUT_SECONDS = 300.0


class WhisperSegment(Protocol):
    text: str


class WhisperModel(Protocol):
    def transcribe(
        self,
        audio_source: io.BytesIO,
        *,
        language: str,
    ) -> tuple[Iterable[WhisperSegment], object]: ...


class WhisperServiceError(RuntimeError):
    error_code = "stt_worker_failed"


class WhisperCapacityError(WhisperServiceError):
    error_code = "stt_capacity_exceeded"


class WhisperInferenceTimeoutError(WhisperServiceError):
    error_code = "stt_inference_timeout"


class WhisperNotReadyError(WhisperServiceError):
    error_code = "stt_not_ready"


def _pcm_to_wav(audio: bytes) -> io.BytesIO:
    source = io.BytesIO()
    with wave.open(source, "wb") as output:
        output.setnchannels(PCM_CHANNELS)
        output.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
        output.setframerate(PCM_SAMPLE_RATE_HZ)
        output.writeframes(audio)
    source.seek(0)
    return source


def _worker_main(connection: Connection, config: WhisperServiceConfig) -> None:
    try:
        from faster_whisper import WhisperModel as FasterWhisperModel

        model = cast(
            WhisperModel,
            FasterWhisperModel(
                config.model_path,
                download_root=config.model_cache,
                device=config.device,
                device_index=config.device_index,
                compute_type=config.compute_type,
                local_files_only=True,
            ),
        )
        # generatorを最後まで消費することでCUDA実推論までready gateに含める。
        silence = bytes(PCM_SAMPLE_RATE_HZ * PCM_CHANNELS * PCM_SAMPLE_WIDTH_BYTES // 10)
        segments, _info = model.transcribe(_pcm_to_wav(silence), language="ja")
        "".join(segment.text for segment in segments)
        connection.send(("ready",))
        while True:
            request = connection.recv()
            if request is None:
                return
            request_id, audio = request
            try:
                segments, _info = model.transcribe(_pcm_to_wav(audio), language="ja")
                transcript = "".join(segment.text for segment in segments)
                connection.send((request_id, "ok", transcript))
            except BaseException as error:
                connection.send((request_id, "error", type(error).__name__))
    except BaseException as error:
        try:
            connection.send(("startup_error", type(error).__name__))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class WhisperWorkerManager:
    """GPU modelを1 processだけ所有し、queueなしsingle-flightで公開する。"""

    def __init__(
        self,
        config: WhisperServiceConfig,
        *,
        process_start_method: str = "spawn",
        startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self._context: Any = multiprocessing.get_context(process_start_method)
        self._startup_timeout_seconds = startup_timeout_seconds
        self._capacity = threading.Lock()
        self._state_lock = threading.RLock()
        self._worker: multiprocessing.Process | None = None
        self._connection: Connection | None = None
        self._ready = False
        self._closed = False
        self._next_request_id = 1

    @property
    def ready(self) -> bool:
        with self._state_lock:
            return self._ready and self._worker is not None and self._worker.is_alive()

    def start(self) -> None:
        with self._state_lock:
            if self._closed:
                raise WhisperNotReadyError("Whisper GPU worker manager is closed")
            if self.ready:
                return
            self._discard_locked("startup")
            parent, child = self._context.Pipe(duplex=True)
            worker = self._context.Process(
                target=_worker_main,
                args=(child, self.config),
                daemon=True,
                name="digital-souls-whisper-gpu",
            )
            worker.start()
            child.close()
            self._worker = worker
            self._connection = parent
        if not parent.poll(self._startup_timeout_seconds):
            self._discard("startup_timeout")
            raise WhisperNotReadyError("Whisper GPU worker startup timed out")
        try:
            response = parent.recv()
        except (EOFError, OSError) as error:
            self._discard("startup_io_failed")
            raise WhisperNotReadyError(
                "Whisper GPU worker failed during startup"
            ) from error
        if response != ("ready",):
            self._discard("startup_failed")
            raise WhisperNotReadyError("Whisper GPU worker failed readiness inference")
        with self._state_lock:
            self._ready = True
        logger.info(
            "Whisper GPU worker ready: model=%s device=%s compute_type=%s device_index=%d",
            self.config.model,
            self.config.device,
            self.config.compute_type,
            self.config.device_index,
        )

    def transcribe(self, audio: bytes) -> str:
        if not self._capacity.acquire(blocking=False):
            raise WhisperCapacityError("Whisper global capacity exceeded")
        try:
            with self._state_lock:
                if not self.ready or self._connection is None:
                    raise WhisperNotReadyError("Whisper GPU worker is not ready")
                connection = self._connection
                request_id = self._next_request_id
                self._next_request_id += 1
            try:
                connection.send((request_id, bytes(audio)))
                if not connection.poll(self.config.inference_timeout_seconds):
                    self._discard("inference_timeout")
                    self._recover_async()
                    raise WhisperInferenceTimeoutError("Whisper inference timed out")
                response = connection.recv()
            except WhisperServiceError:
                raise
            except (BrokenPipeError, EOFError, OSError) as error:
                self._discard("worker_io_failed")
                self._recover_async()
                raise WhisperServiceError("Whisper worker communication failed") from error
            if (
                not isinstance(response, tuple)
                or len(response) < 3
                or response[0] != request_id
            ):
                self._discard("malformed_response")
                self._recover_async()
                raise WhisperServiceError("Whisper worker response is invalid")
            if response[1] == "ok" and isinstance(response[2], str):
                return response[2]
            self._discard("inference_failed")
            self._recover_async()
            raise WhisperServiceError("Whisper inference failed")
        finally:
            self._capacity.release()

    def close(self) -> None:
        with self._capacity:
            with self._state_lock:
                self._closed = True
            self._discard("shutdown")

    def _recover_async(self) -> None:
        threading.Thread(
            target=self._recover,
            daemon=True,
            name="digital-souls-whisper-recovery",
        ).start()

    def _recover(self) -> None:
        with self._state_lock:
            if self._closed:
                return
        try:
            self.start()
        except Exception as error:
            logger.error(
                "Whisper GPU worker recovery failed: error_type=%s",
                type(error).__name__,
            )

    def _discard(self, reason: str) -> None:
        with self._state_lock:
            self._discard_locked(reason)

    def _discard_locked(self, reason: str) -> None:
        connection = self._connection
        worker = self._worker
        self._connection = None
        self._worker = None
        self._ready = False
        if connection is not None:
            try:
                connection.send(None)
            except (BrokenPipeError, EOFError, OSError):
                pass
            connection.close()
        if worker is not None:
            worker.join(timeout=1)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=1)
        if reason not in {"startup", "shutdown"}:
            logger.warning("Whisper GPU worker discarded: reason=%s", reason)
