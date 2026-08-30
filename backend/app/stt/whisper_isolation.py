from __future__ import annotations

import logging
import multiprocessing
import os
import threading
from multiprocessing.connection import Connection
from pathlib import Path
from time import monotonic
from typing import Any, cast

from app.stt.whisper_client import WhisperTranscriber

logger = logging.getLogger(__name__)
WHISPER_LOCK_TIMEOUT_SECONDS_ENV = "WHISPER_LOCK_TIMEOUT_SECONDS"
WHISPER_INFERENCE_TIMEOUT_SECONDS_ENV = "WHISPER_INFERENCE_TIMEOUT_SECONDS"
DEFAULT_WHISPER_LOCK_TIMEOUT_SECONDS = 5.0
DEFAULT_WHISPER_INFERENCE_TIMEOUT_SECONDS = 45.0


class WhisperIsolationError(RuntimeError):
    """隔離workerを再利用できないSTT障害。"""

    error_code = "stt_worker_failed"


class WhisperTimeoutError(WhisperIsolationError):
    def __init__(self, phase: str) -> None:
        super().__init__(f"Whisper {phase} timeout")
        self.phase = phase
        self.error_code = f"stt_{phase}_timeout"


def _worker_main(
    connection: Connection,
    model_name: str,
    download_root: str,
) -> None:
    transcriber = WhisperTranscriber(
        model_name=model_name,
        download_root=Path(download_root),
    )
    try:
        while True:
            request = connection.recv()
            if request is None:
                return
            request_id, audio = request
            try:
                connection.send((request_id, "ok", transcriber.transcribe(audio)))
            except BaseException as error:
                connection.send(
                    (request_id, "error", type(error).__name__, str(error))
                )
    except (EOFError, BrokenPipeError):
        return
    finally:
        connection.close()


class IsolatedWhisperTranscriber:
    """native推論を破棄可能な単一processに封じ込める。

    同時requestは親processのlockで1件に制限する。lock待ちまたは推論が
    timeoutした場合、実行中workerをterminateして次回requestで再生成する。
    """

    def __init__(
        self,
        *,
        model_name: str,
        download_root: Path,
        lock_timeout_seconds: float = 5.0,
        inference_timeout_seconds: float = 45.0,
        process_start_method: str = "spawn",
    ) -> None:
        if lock_timeout_seconds <= 0 or inference_timeout_seconds <= 0:
            raise ValueError("Whisper timeout values must be positive")
        self._model_name = model_name
        self._download_root = download_root
        self._lock_timeout_seconds = lock_timeout_seconds
        self._inference_timeout_seconds = inference_timeout_seconds
        self._context: Any = multiprocessing.get_context(process_start_method)
        self._request_lock = threading.Lock()
        self._worker_state_lock = threading.RLock()
        self._worker: multiprocessing.Process | None = None
        self._connection: Connection | None = None
        self._next_request_id = 1

    def transcribe(self, audio: bytes) -> str:
        lock_started = monotonic()
        if not self._request_lock.acquire(timeout=self._lock_timeout_seconds):
            logger.warning(
                "Whisper request isolated: phase=lock_wait_timeout elapsed_ms=%d",
                int((monotonic() - lock_started) * 1000),
            )
            # lock保持中のnative推論ごとprocessを破棄し、後続requestが
            # 同じresourceを待ち続けないようにする。
            self._discard_worker("lock_wait_timeout")
            raise WhisperTimeoutError("lock_wait")
        try:
            connection = self._ensure_worker()
            request_id = self._next_request_id
            self._next_request_id += 1
            try:
                connection.send((request_id, bytes(audio)))
            except (BrokenPipeError, EOFError, OSError) as error:
                self._discard_worker("worker_send_failed")
                raise WhisperIsolationError("Whisper worker send failed") from error
            try:
                response_ready = connection.poll(self._inference_timeout_seconds)
            except (BrokenPipeError, EOFError, OSError) as error:
                self._discard_worker("worker_poll_failed")
                raise WhisperIsolationError("Whisper worker poll failed") from error
            if not response_ready:
                self._discard_worker("inference_timeout")
                raise WhisperTimeoutError("inference")
            try:
                response = connection.recv()
            except (BrokenPipeError, EOFError, OSError) as error:
                self._discard_worker("worker_receive_failed")
                raise WhisperIsolationError("Whisper worker receive failed") from error
            if not isinstance(response, tuple) or len(response) < 3:
                self._discard_worker("malformed_worker_response")
                raise WhisperIsolationError("Whisper worker response is malformed")
            if response[0] != request_id:
                self._discard_worker("worker_response_mismatch")
                raise WhisperIsolationError("Whisper worker response id mismatch")
            if response[1] == "ok" and isinstance(response[2], str):
                return response[2]
            self._discard_worker("inference_failed")
            detail = response[3] if len(response) > 3 else "unknown error"
            raise WhisperIsolationError(f"Whisper inference failed: {detail}")
        finally:
            self._request_lock.release()

    def close(self) -> None:
        with self._request_lock:
            with self._worker_state_lock:
                connection = self._connection
                worker = self._worker
                self._connection = None
                self._worker = None
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

    def _ensure_worker(self) -> Connection:
        with self._worker_state_lock:
            if (
                self._worker is not None
                and self._worker.is_alive()
                and self._connection is not None
            ):
                return self._connection
            self._discard_worker("worker_recreated")
            parent, child = self._context.Pipe(duplex=True)
            worker: multiprocessing.Process | None = None
            try:
                worker = self._context.Process(
                    target=_worker_main,
                    args=(child, self._model_name, str(self._download_root)),
                    daemon=True,
                    name="digital-souls-whisper",
                )
                worker.start()
            except BaseException as error:
                parent.close()
                child.close()
                if worker is not None and worker.pid is not None:
                    if worker.is_alive():
                        worker.terminate()
                    worker.join(timeout=1)
                logger.warning(
                    "Whisper isolated worker unavailable: "
                    "reason=worker_start_failed error_type=%s",
                    type(error).__name__,
                )
                raise WhisperIsolationError(
                    "Whisper worker could not be created"
                ) from error
            child.close()
            self._worker = worker
            self._connection = parent
            logger.info("Whisper isolated worker created: pid=%s", worker.pid)
            return cast(Connection, parent)

    def _discard_worker(self, reason: str) -> None:
        with self._worker_state_lock:
            connection = self._connection
            worker = self._worker
            self._connection = None
            self._worker = None
            if connection is not None:
                connection.close()
            if worker is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=1)
            if reason != "worker_recreated" or worker is not None:
                logger.warning(
                    "Whisper isolated worker discarded: reason=%s pid=%s",
                    reason,
                    None if worker is None else worker.pid,
                )


def create_isolated_whisper_transcriber(
    *, model_name: str, download_root: Path
) -> IsolatedWhisperTranscriber:
    return IsolatedWhisperTranscriber(
        model_name=model_name,
        download_root=download_root,
        lock_timeout_seconds=_positive_timeout(
            os.environ.get(WHISPER_LOCK_TIMEOUT_SECONDS_ENV),
            DEFAULT_WHISPER_LOCK_TIMEOUT_SECONDS,
            WHISPER_LOCK_TIMEOUT_SECONDS_ENV,
        ),
        inference_timeout_seconds=_positive_timeout(
            os.environ.get(WHISPER_INFERENCE_TIMEOUT_SECONDS_ENV),
            DEFAULT_WHISPER_INFERENCE_TIMEOUT_SECONDS,
            WHISPER_INFERENCE_TIMEOUT_SECONDS_ENV,
        ),
    )


def _positive_timeout(value: str | None, default: float, field: str) -> float:
    if value is None:
        return default
    try:
        timeout = float(value)
    except ValueError as error:
        raise ValueError(f"{field} must be a positive number") from error
    if timeout <= 0:
        raise ValueError(f"{field} must be a positive number")
    return timeout
