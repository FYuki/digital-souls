import logging
import math
import os
import threading
import time
from typing import cast

import httpx

from app.tts.speech_synthesizer import SpeechSynthesisError

VOICEVOX_BASE_URL_ENV = "VOICEVOX_BASE_URL"
DEFAULT_VOICEVOX_BASE_URL = "http://127.0.0.1:50021"
VOICEVOX_TIMEOUT_SECONDS = 30.0
VOICEVOX_SHUTDOWN_DRAIN_SECONDS_ENV = "VOICEVOX_SHUTDOWN_DRAIN_SECONDS"
DEFAULT_VOICEVOX_SHUTDOWN_DRAIN_SECONDS = 35.0
AUDIO_QUERY_PATH = "/audio_query"
SYNTHESIS_PATH = "/synthesis"
TEXT_PARAM = "text"
SPEAKER_PARAM = "speaker"
JsonObject = dict[str, object]
logger = logging.getLogger(__name__)


class VoicevoxClient:
    def __init__(
        self,
        base_url: str,
        *,
        shutdown_drain_seconds: float = DEFAULT_VOICEVOX_SHUTDOWN_DRAIN_SECONDS,
    ) -> None:
        if not math.isfinite(shutdown_drain_seconds) or shutdown_drain_seconds < 0:
            raise ValueError("shutdown_drain_seconds must be non-negative")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=httpx.Timeout(VOICEVOX_TIMEOUT_SECONDS))
        self._shutdown_drain_seconds = shutdown_drain_seconds
        self._condition = threading.Condition()
        self._accepting = True
        self._inflight = 0
        self._client_closing = False
        self._client_closed = False
        self._request_sequence = 0

    def synthesize(self, text: str, speaker_id: int) -> bytes:
        request_id = self._begin_request()
        deadline = time.monotonic() + VOICEVOX_TIMEOUT_SECONDS
        outcome = "completed"
        try:
            audio_query = self._create_audio_query(text, speaker_id, deadline)
            return self._synthesize_audio(audio_query, speaker_id, deadline)
        except httpx.TimeoutException:
            outcome = "request_timeout"
            # request URLのqueryには合成本文が含まれ得るためcauseを外へ公開しない。
            raise SpeechSynthesisError("VOICEVOX request failed") from None
        except httpx.ConnectError:
            outcome = "connection_failed"
            # request URLのqueryには合成本文が含まれ得るためcauseを外へ公開しない。
            raise SpeechSynthesisError("VOICEVOX request failed") from None
        except httpx.HTTPError:
            outcome = "request_failed"
            # request URLのqueryには合成本文が含まれ得るためcauseを外へ公開しない。
            raise SpeechSynthesisError("VOICEVOX request failed") from None
        except Exception:
            outcome = "synthesis_failed"
            raise
        finally:
            logger.info(
                "VOICEVOX synthesis lifecycle: request_id=%s outcome=%s",
                request_id,
                outcome,
            )
            self._finish_request()

    def close(self) -> bool:
        deadline = time.monotonic() + self._shutdown_drain_seconds
        close_client = False
        with self._condition:
            self._accepting = False
            while self._inflight > 0 or self._client_closing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "VOICEVOX shutdown lifecycle: outcome=shutdown_timeout inflight=%s",
                        self._inflight,
                    )
                    return False
                self._condition.wait(timeout=remaining)
            if not self._client_closed:
                self._client_closing = True
                close_client = True
        if close_client:
            self._close_client()
        logger.info("VOICEVOX shutdown lifecycle: outcome=drained inflight=0")
        return True

    @property
    def inflight(self) -> int:
        with self._condition:
            return self._inflight

    def _begin_request(self) -> int:
        with self._condition:
            if not self._accepting or self._client_closed:
                raise SpeechSynthesisError("VOICEVOX client is shutting down")
            self._inflight += 1
            self._request_sequence += 1
            return self._request_sequence

    def _finish_request(self) -> None:
        close_client = False
        with self._condition:
            self._inflight -= 1
            if self._inflight < 0:
                raise RuntimeError("VOICEVOX in-flight count became negative")
            if self._inflight == 0:
                self._condition.notify_all()
                if (
                    not self._accepting
                    and not self._client_closing
                    and not self._client_closed
                ):
                    self._client_closing = True
                    close_client = True
        if close_client:
            self._close_client()
            logger.info(
                "VOICEVOX shutdown lifecycle: outcome=drained_after_timeout inflight=0"
            )

    def _close_client(self) -> None:
        try:
            self._client.close()
        except Exception:
            with self._condition:
                self._client_closing = False
                self._condition.notify_all()
            raise
        with self._condition:
            self._client_closing = False
            self._client_closed = True
            self._condition.notify_all()

    @staticmethod
    def _remaining_timeout(deadline: float) -> httpx.Timeout:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpx.TimeoutException("VOICEVOX synthesis deadline exceeded")
        return httpx.Timeout(remaining)

    def _create_audio_query(
        self,
        text: str,
        speaker_id: int,
        deadline: float,
    ) -> JsonObject:
        response = self._client.post(
            f"{self._base_url}{AUDIO_QUERY_PATH}",
            params={TEXT_PARAM: text, SPEAKER_PARAM: speaker_id},
            timeout=self._remaining_timeout(deadline),
        )
        response.raise_for_status()
        audio_query = response.json()
        if not isinstance(audio_query, dict):
            raise SpeechSynthesisError(
                "VOICEVOX audio_query response must be a JSON object"
            )
        return cast(JsonObject, audio_query)

    def _synthesize_audio(
        self,
        audio_query: JsonObject,
        speaker_id: int,
        deadline: float,
    ) -> bytes:
        response = self._client.post(
            f"{self._base_url}{SYNTHESIS_PATH}",
            params={SPEAKER_PARAM: speaker_id},
            json=audio_query,
            timeout=self._remaining_timeout(deadline),
        )
        response.raise_for_status()
        return response.content


def create_voicevox_client(base_url: str) -> VoicevoxClient:
    configured = os.environ.get(VOICEVOX_SHUTDOWN_DRAIN_SECONDS_ENV)
    drain_seconds = (
        DEFAULT_VOICEVOX_SHUTDOWN_DRAIN_SECONDS
        if configured is None or configured.strip() == ""
        else float(configured)
    )
    return VoicevoxClient(base_url, shutdown_drain_seconds=drain_seconds)
