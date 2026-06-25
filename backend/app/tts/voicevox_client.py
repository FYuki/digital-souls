from typing import cast

import httpx

VOICEVOX_BASE_URL_ENV = "VOICEVOX_BASE_URL"
DEFAULT_VOICEVOX_BASE_URL = "http://localhost:50021"
VOICEVOX_TIMEOUT_SECONDS = 30.0
AUDIO_QUERY_PATH = "/audio_query"
SYNTHESIS_PATH = "/synthesis"
TEXT_PARAM = "text"
SPEAKER_PARAM = "speaker"


def synthesize(text: str, speaker_id: int, base_url: str) -> bytes:
    normalized_base_url = base_url.rstrip("/")
    audio_query = _create_audio_query(text, speaker_id, normalized_base_url)
    return _synthesize_audio(audio_query, speaker_id, normalized_base_url)


JsonObject = dict[str, object]


def _voicevox_timeout() -> httpx.Timeout:
    return httpx.Timeout(VOICEVOX_TIMEOUT_SECONDS)


def _create_audio_query(text: str, speaker_id: int, base_url: str) -> JsonObject:
    response = httpx.post(
        f"{base_url}{AUDIO_QUERY_PATH}",
        params={TEXT_PARAM: text, SPEAKER_PARAM: speaker_id},
        timeout=_voicevox_timeout(),
    )
    response.raise_for_status()
    audio_query = response.json()
    if not isinstance(audio_query, dict):
        raise ValueError("VOICEVOX audio_query response must be a JSON object")
    return cast(JsonObject, audio_query)


def _synthesize_audio(audio_query: JsonObject, speaker_id: int, base_url: str) -> bytes:
    response = httpx.post(
        f"{base_url}{SYNTHESIS_PATH}",
        params={SPEAKER_PARAM: speaker_id},
        json=audio_query,
        timeout=_voicevox_timeout(),
    )
    response.raise_for_status()
    return response.content
