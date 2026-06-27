from typing import cast

import httpx

from app.tts.speech_synthesizer import SpeechSynthesisError

VOICEVOX_BASE_URL_ENV = "VOICEVOX_BASE_URL"
DEFAULT_VOICEVOX_BASE_URL = "http://localhost:50021"
VOICEVOX_TIMEOUT_SECONDS = 30.0
AUDIO_QUERY_PATH = "/audio_query"
SYNTHESIS_PATH = "/synthesis"
TEXT_PARAM = "text"
SPEAKER_PARAM = "speaker"
JsonObject = dict[str, object]


class VoicevoxClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=httpx.Timeout(VOICEVOX_TIMEOUT_SECONDS))

    def synthesize(self, text: str, speaker_id: int) -> bytes:
        try:
            audio_query = self._create_audio_query(text, speaker_id)
            return self._synthesize_audio(audio_query, speaker_id)
        except httpx.HTTPError as exc:
            raise SpeechSynthesisError("VOICEVOX request failed") from exc

    def close(self) -> None:
        self._client.close()

    def _create_audio_query(
        self,
        text: str,
        speaker_id: int,
    ) -> JsonObject:
        response = self._client.post(
            f"{self._base_url}{AUDIO_QUERY_PATH}",
            params={TEXT_PARAM: text, SPEAKER_PARAM: speaker_id},
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
    ) -> bytes:
        response = self._client.post(
            f"{self._base_url}{SYNTHESIS_PATH}",
            params={SPEAKER_PARAM: speaker_id},
            json=audio_query,
        )
        response.raise_for_status()
        return response.content


def create_voicevox_client(base_url: str) -> VoicevoxClient:
    return VoicevoxClient(base_url)
