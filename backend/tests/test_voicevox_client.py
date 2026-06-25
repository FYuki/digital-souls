from unittest.mock import MagicMock, patch

import httpx
import pytest


def _json_response(body: dict) -> MagicMock:
    response = MagicMock()
    response.json.return_value = body
    response.raise_for_status.return_value = None
    return response


def _audio_response(content: bytes) -> MagicMock:
    response = MagicMock()
    response.content = content
    response.raise_for_status.return_value = None
    return response


_PATCH_HTTPX_POST = "app.tts.voicevox_client.httpx.post"


class TestVoicevoxClientSynthesize:
    def test_posts_audio_query_then_synthesis_and_returns_wav_bytes(self):
        audio_query = {"accent_phrases": [], "speedScale": 1.0}
        wav_bytes = b"RIFF....WAVE"

        from app.tts.voicevox_client import synthesize

        with patch(
            _PATCH_HTTPX_POST,
            side_effect=[_json_response(audio_query), _audio_response(wav_bytes)],
        ) as mock_post:
            result = synthesize("こんにちは", 14, "http://voicevox.local:50021")

        assert result == wav_bytes
        audio_query_call, synthesis_call = mock_post.call_args_list
        assert audio_query_call.args[0] == "http://voicevox.local:50021/audio_query"
        assert audio_query_call.kwargs["params"] == {
            "text": "こんにちは",
            "speaker": 14,
        }
        assert synthesis_call.args[0] == "http://voicevox.local:50021/synthesis"
        assert synthesis_call.kwargs["params"] == {"speaker": 14}
        assert synthesis_call.kwargs["json"] == audio_query

    def test_passes_explicit_timeout_to_voicevox_requests(self):
        from app.tts.voicevox_client import synthesize

        with patch(
            _PATCH_HTTPX_POST,
            side_effect=[_json_response({}), _audio_response(b"RIFF")],
        ) as mock_post:
            synthesize("こんにちは", 14, "http://voicevox.local:50021")

        for call in mock_post.call_args_list:
            timeout = call.kwargs["timeout"]
            assert isinstance(timeout, httpx.Timeout)
            assert timeout.connect == 30.0

    def test_timeout_factory_is_not_public_api(self):
        import app.tts.voicevox_client as voicevox_client

        assert not hasattr(voicevox_client, "voicevox_timeout")

    def test_raises_http_status_error_before_synthesis_when_audio_query_fails(
        self,
    ):
        from app.tts.voicevox_client import synthesize

        request = httpx.Request("POST", "http://voicevox.local:50021/audio_query")
        failed_response = _json_response({})
        failed_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "server error",
            request=request,
            response=httpx.Response(500, request=request),
        )

        with patch(_PATCH_HTTPX_POST, return_value=failed_response) as mock_post:
            with pytest.raises(httpx.HTTPStatusError):
                synthesize("こんにちは", 14, "http://voicevox.local:50021")

        assert mock_post.call_count == 1
        failed_response.json.assert_not_called()

    def test_strips_trailing_slash_from_explicit_base_url(self):
        from app.tts.voicevox_client import synthesize

        with patch(
            _PATCH_HTTPX_POST,
            side_effect=[_json_response({}), _audio_response(b"RIFF")],
        ) as mock_post:
            synthesize("こんにちは", 14, "http://voicevox.local:50021/")

        called_url = mock_post.call_args_list[0].args[0]
        assert called_url == "http://voicevox.local:50021/audio_query"
