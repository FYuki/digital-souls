from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import multiprocessing
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as ReceiveTimeoutError
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from irodori_service.config import (
    CODEC_REPOSITORY,
    CODEC_REVISION,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    ServiceConfig,
)
from irodori_service.contracts import ServiceError, SpeechRequest, validate_wav
from irodori_service.voices import RegisteredVoices

logger = logging.getLogger(__name__)


def gpu_worker(connection: Connection, config: ServiceConfig) -> None:
    """作者の通常API関数を単一の隔離processから使用する。"""
    # 外部runtimeが本文をlogへ含めても、serviceのログ・APIへ流さない。
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        logging.disable(logging.CRITICAL)
        try:
            snapshot_download = importlib.import_module("huggingface_hub").snapshot_download

            model = Path(snapshot_download(
                repo_id=MODEL_REPOSITORY, revision=MODEL_REVISION,
                cache_dir=str(config.model_cache),
                allow_patterns=["model.safetensors", "tokenizer/*"],
            ))
            codec = Path(snapshot_download(
                repo_id=CODEC_REPOSITORY, revision=CODEC_REVISION,
                cache_dir=str(config.model_cache), allow_patterns=["weights.pth"],
            ))
            os.environ.update({
                "IRODORI_CHECKPOINT": str(model / "model.safetensors"),
                "IRODORI_CODEC_REPO": str(codec / "weights.pth"),
                "IRODORI_MODEL_DEVICE": "cuda", "IRODORI_CODEC_DEVICE": "cuda",
                "IRODORI_MODEL_PRECISION": "bf16", "IRODORI_CODEC_PRECISION": "bf16",
                "IRODORI_PRELOAD": "false", "IRODORI_ALLOW_NO_REF_VOICE": "false",
                "IRODORI_VOICES_DIR": str(config.voices_dir),
                "IRODORI_DEFAULT_CHUNKING_ENABLED": "false",
                "IRODORI_MAX_CONCURRENT_SYNTHESIS": "1",
                "IRODORI_MODEL_LOAD_TIMEOUT": str(config.startup_timeout),
            })
            upstream = importlib.import_module("irodori_openai_tts.app")
            if config.cuda_graph:
                if upstream.settings.compile_model:
                    raise RuntimeError("irodori_graph_compile_conflict")
                from irodori_service.cuda_graph import install_cuda_graph_sampler

                install_cuda_graph_sampler()
            UpstreamRequest, create_speech = upstream.SpeechRequest, upstream.create_speech

            voices = RegisteredVoices(config.voices_dir)
            with contextlib.closing(asyncio.new_event_loop()) as loop:
                asyncio.set_event_loop(loop)
                def synthesize(payload: SpeechRequest) -> bytes:
                    voices.resolve(payload.voice)
                    response = loop.run_until_complete(create_speech(UpstreamRequest(**payload.model_dump())))
                    audio = bytes(response.body)
                    validate_wav(audio)
                    return audio

                synthesize(voices.warmup_request(config.warmup_voice))
                connection.send(("ready",))
                while True:
                    payload = connection.recv()
                    if payload is None:
                        break
                    try:
                        audio = synthesize(SpeechRequest.model_validate(payload))
                        connection.send(("ok", audio))
                    except Exception:  # noqa: BLE001 - 外部runtimeの詳細を公開せず安定した失敗へ変換する
                        # 応答本文・例外詳細は親へ渡さず、親がprocessを再生成する。
                        connection.send(("error", "tts_inference_failed"))
        except BaseException:  # noqa: BLE001 - 所有processの終了時にも親へ失敗だけを通知する
            try:
                connection.send(("error", "tts_worker_start_failed"))
            except (OSError, EOFError):
                pass
        finally:
            connection.close()


class ProcessWorker:
    """timeout時に自分が所有するprocessだけを停止し、GPU実処理を残さない。"""

    def __init__(
        self, config: ServiceConfig,
        target: Callable[[Connection, ServiceConfig], None] = gpu_worker,
    ) -> None:
        self.config = config
        self._target = target
        self._context = multiprocessing.get_context("spawn")
        self._lock = threading.RLock()
        self._worker: Any = None
        self._connection: Connection | None = None
        self._ready = False
        self._closed = False
        self.preparation_seconds: float | None = None

    @property
    def ready(self) -> bool:
        with self._lock:
            return bool(self._ready and self._worker and self._worker.is_alive())

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise ServiceError("tts_not_ready", 503)
            if self.ready:
                return
            self._discard_locked()
            parent, child = self._context.Pipe()
            self._worker = self._context.Process(
                target=self._target, args=(child, self.config), daemon=True,
                name="digital-souls-irodori-gpu",
            )
            self._connection = parent
            started = time.monotonic()
            self._worker.start()
            child.close()
        try:
            result = self._receive(parent, self.config.startup_timeout, "tts_startup_timeout")
            if result != ("ready",):
                raise ServiceError("tts_preparation_failed", 503)
            with self._lock:
                if self._closed or self._connection is not parent:
                    raise ServiceError("tts_not_ready", 503)
                self.preparation_seconds = time.monotonic() - started
                self._ready = True
        except BaseException:
            with self._lock:
                self._discard_locked()
            raise

    def synthesize(self, payload: SpeechRequest) -> bytes:
        with self._lock:
            if not self.ready or self._connection is None:
                raise ServiceError("tts_not_ready", 503)
            connection = self._connection
        try:
            connection.send(payload.model_dump())
            result = self._receive(connection, self.config.inference_timeout, "tts_inference_timeout")
            if not isinstance(result, tuple) or len(result) != 2 or result[0] != "ok":
                raise ServiceError("tts_inference_failed")
            if not isinstance(result[1], bytes):
                raise ServiceError("tts_invalid_audio")
            validate_wav(result[1])
            return result[1]
        except BaseException:
            with self._lock:
                self._discard_locked()
            raise

    def _receive(self, connection: Connection, timeout: float, code: str) -> object:
        # pollの期限だけでは、header受信後に本文転送が止まるとrecvが無期限に待つ。
        # 返信全体の受信を別threadで待ち、期限切れは呼出元で所有processごと終了する。
        # processとConnectionの終了により、部分frameを待つthreadも解放される。
        received: Future[object] = Future()

        def receive() -> None:
            try:
                received.set_result(connection.recv())
            except Exception as error:  # noqa: BLE001 - 呼出元へ元の受信失敗を渡す
                received.set_exception(error)

        threading.Thread(target=receive, name="irodori-worker-reply", daemon=True).start()
        try:
            return received.result(timeout=timeout)
        except ReceiveTimeoutError as error:
            raise ServiceError(code, 504) from error
        except (EOFError, OSError) as error:
            raise ServiceError("tts_worker_failed") from error

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._discard_locked()

    def _discard_locked(self) -> None:
        self._ready = False
        worker, self._worker = self._worker, None
        connection, self._connection = self._connection, None
        if worker is not None:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=2)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=2)
            if worker.is_alive():
                # 生存したGPU workerを残して別processを起動しない。
                self._worker = worker
                self._closed = True
                raise ServiceError("tts_worker_stop_failed", 503)
            worker.close()
        if connection is not None:
            connection.close()
