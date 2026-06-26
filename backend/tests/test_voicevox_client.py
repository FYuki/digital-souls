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


class TestVoicevoxClientSynthesize:
    def test_posts_audio_query_then_synthesis_and_returns_wav_bytes(self):
        audio_query = {"accent_phrases": [], "speedScale": 1.0}
        wav_bytes = b"RIFF....WAVE"

        from app.tts.voicevox_client import create_voicevox_client

        voicevox_client = create_voicevox_client("http://voicevox.local:50021")
        with patch.object(
            voicevox_client._client,
            "post",
            side_effect=[_json_response(audio_query), _audio_response(wav_bytes)],
        ) as mock_post:
            result = voicevox_client.synthesize("こんにちは", 14)

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

    def test_configures_reused_client_with_voicevox_timeout(self):
        from app.tts.voicevox_client import create_voicevox_client

        voicevox_client = create_voicevox_client("http://voicevox.local:50021")

        assert isinstance(voicevox_client._client, httpx.Client)
        assert voicevox_client._client.timeout.connect == 30.0

    def test_reuses_module_client_without_per_request_timeout_kwargs(self):
        from app.tts.voicevox_client import create_voicevox_client

        voicevox_client = create_voicevox_client("http://voicevox.local:50021")
        with patch.object(
            voicevox_client._client,
            "post",
            side_effect=[_json_response({}), _audio_response(b"RIFF")],
        ) as mock_post:
            voicevox_client.synthesize("こんにちは", 14)

        assert mock_post.call_count == 2
        assert all("timeout" not in call.kwargs for call in mock_post.call_args_list)

    def test_close_closes_reused_client(self):
        from app.tts.voicevox_client import create_voicevox_client

        voicevox_client = create_voicevox_client("http://voicevox.local:50021")

        with patch.object(voicevox_client._client, "close") as close:
            voicevox_client.close()

        close.assert_called_once_with()

    def test_timeout_factory_is_not_public_api(self):
        import app.tts.voicevox_client as voicevox_client

        assert not hasattr(voicevox_client, "_client")
        assert not hasattr(voicevox_client, "voicevox_timeout")
        assert not hasattr(voicevox_client, "_voicevox_timeout")

    def test_wraps_http_status_error_before_synthesis_when_audio_query_fails(
        self,
    ):
        from app.tts.speech_synthesizer import SpeechSynthesisError
        from app.tts.voicevox_client import create_voicevox_client

        request = httpx.Request("POST", "http://voicevox.local:50021/audio_query")
        failed_response = _json_response({})
        failed_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "server error",
            request=request,
            response=httpx.Response(500, request=request),
        )

        voicevox_client = create_voicevox_client("http://voicevox.local:50021")
        with patch.object(
            voicevox_client._client,
            "post",
            return_value=failed_response,
        ) as mock_post:
            with pytest.raises(SpeechSynthesisError, match="VOICEVOX request failed"):
                voicevox_client.synthesize("こんにちは", 14)

        assert mock_post.call_count == 1
        failed_response.json.assert_not_called()

    def test_wraps_invalid_audio_query_shape_as_synthesis_error(self):
        from app.tts.speech_synthesizer import SpeechSynthesisError
        from app.tts.voicevox_client import create_voicevox_client

        voicevox_client = create_voicevox_client("http://voicevox.local:50021")
        with patch.object(
            voicevox_client._client,
            "post",
            return_value=_json_response([]),
        ):
            with pytest.raises(
                SpeechSynthesisError,
                match="VOICEVOX audio_query response must be a JSON object",
            ):
                voicevox_client.synthesize("こんにちは", 14)

    def test_strips_trailing_slash_from_explicit_base_url(self):
        from app.tts.voicevox_client import create_voicevox_client

        voicevox_client = create_voicevox_client("http://voicevox.local:50021/")
        with patch.object(
            voicevox_client._client,
            "post",
            side_effect=[_json_response({}), _audio_response(b"RIFF")],
        ) as mock_post:
            voicevox_client.synthesize("こんにちは", 14)

        called_url = mock_post.call_args_list[0].args[0]
        assert called_url == "http://voicevox.local:50021/audio_query"
