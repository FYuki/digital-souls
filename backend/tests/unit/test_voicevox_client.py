import logging
import threading
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

    def test_applies_remaining_overall_deadline_to_each_request(self):
        from app.tts.voicevox_client import create_voicevox_client

        voicevox_client = create_voicevox_client("http://voicevox.local:50021")
        with patch.object(
            voicevox_client._client,
            "post",
            side_effect=[_json_response({}), _audio_response(b"RIFF")],
        ) as mock_post:
            voicevox_client.synthesize("こんにちは", 14)

        assert mock_post.call_count == 2
        assert all(
            isinstance(call.kwargs["timeout"], httpx.Timeout)
            for call in mock_post.call_args_list
        )

    def test_close_closes_reused_client(self):
        from app.tts.voicevox_client import create_voicevox_client

        voicevox_client = create_voicevox_client("http://voicevox.local:50021")

        with patch.object(voicevox_client._client, "close") as close:
            voicevox_client.close()

        close.assert_called_once_with()

    def test_shutdown_waits_for_inflight_request_before_closing(self):
        from app.tts.voicevox_client import VoicevoxClient

        voicevox_client = VoicevoxClient(
            "http://voicevox.local:50021",
            shutdown_drain_seconds=1.0,
        )
        entered = threading.Event()
        release = threading.Event()

        def blocking_post(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=1.0)
            return _json_response({})

        with (
            patch.object(voicevox_client._client, "post", side_effect=blocking_post),
            patch.object(voicevox_client._client, "close") as close,
        ):
            synthesis = threading.Thread(
                target=lambda: voicevox_client.synthesize("こんにちは", 14)
            )
            synthesis.start()
            assert entered.wait(timeout=1.0)
            shutdown_result: list[bool] = []
            shutdown = threading.Thread(
                target=lambda: shutdown_result.append(voicevox_client.close())
            )
            shutdown.start()

            assert voicevox_client.inflight == 1
            with voicevox_client._condition:
                assert voicevox_client._condition.wait_for(
                    lambda: not voicevox_client._accepting,
                    timeout=1.0,
                )
            from app.tts.speech_synthesizer import SpeechSynthesisError

            with pytest.raises(
                SpeechSynthesisError, match="VOICEVOX client is shutting down"
            ):
                voicevox_client.synthesize("drain中の新規要求", 14)
            close.assert_not_called()
            release.set()
            synthesis.join(timeout=1.0)
            shutdown.join(timeout=1.0)

        assert not synthesis.is_alive()
        assert not shutdown.is_alive()
        assert shutdown_result == [True]
        close.assert_called_once_with()

    def test_shutdown_drains_parallel_chunk_requests_before_closing(self):
        from app.tts.voicevox_client import VoicevoxClient

        voicevox_client = VoicevoxClient(
            "http://voicevox.local:50021",
            shutdown_drain_seconds=1.0,
        )
        entered = threading.Condition()
        entered_count = 0
        release = threading.Event()

        def blocking_post(url, *_args, **_kwargs):
            nonlocal entered_count
            if url.endswith("/audio_query"):
                with entered:
                    entered_count += 1
                    entered.notify_all()
                release.wait(timeout=1.0)
                return _json_response({})
            return _audio_response(b"wav")

        with (
            patch.object(voicevox_client._client, "post", side_effect=blocking_post),
            patch.object(voicevox_client._client, "close") as close,
        ):
            synthesis_threads = [
                threading.Thread(
                    target=lambda text=text: voicevox_client.synthesize(text, 14)
                )
                for text in ("第一節", "第二節")
            ]
            for synthesis in synthesis_threads:
                synthesis.start()
            with entered:
                assert entered.wait_for(lambda: entered_count == 2, timeout=1.0)

            shutdown_result: list[bool] = []
            shutdown = threading.Thread(
                target=lambda: shutdown_result.append(voicevox_client.close())
            )
            shutdown.start()
            assert voicevox_client.inflight == 2
            close.assert_not_called()

            release.set()
            for synthesis in synthesis_threads:
                synthesis.join(timeout=1.0)
            shutdown.join(timeout=1.0)

        assert all(not synthesis.is_alive() for synthesis in synthesis_threads)
        assert not shutdown.is_alive()
        assert shutdown_result == [True]
        close.assert_called_once_with()

    def test_shutdown_success_waits_for_http_client_close_completion(self):
        from app.tts.voicevox_client import VoicevoxClient

        voicevox_client = VoicevoxClient(
            "http://voicevox.local:50021",
            shutdown_drain_seconds=1.0,
        )
        request_entered = threading.Event()
        release_request = threading.Event()
        close_entered = threading.Event()
        release_close = threading.Event()

        def blocking_post(url, *_args, **_kwargs):
            if url.endswith("/audio_query"):
                request_entered.set()
                release_request.wait(timeout=1.0)
                return _json_response({})
            return _audio_response(b"wav")

        def blocking_close() -> None:
            close_entered.set()
            release_close.wait(timeout=1.0)

        with (
            patch.object(voicevox_client._client, "post", side_effect=blocking_post),
            patch.object(voicevox_client._client, "close", side_effect=blocking_close),
        ):
            synthesis = threading.Thread(
                target=lambda: voicevox_client.synthesize("こんにちは", 14)
            )
            synthesis.start()
            assert request_entered.wait(timeout=1.0)
            shutdown_result: list[bool] = []
            shutdown = threading.Thread(
                target=lambda: shutdown_result.append(voicevox_client.close())
            )
            shutdown.start()

            release_request.set()
            assert close_entered.wait(timeout=1.0)
            shutdown.join(timeout=0.05)
            assert shutdown.is_alive()
            assert shutdown_result == []

            release_close.set()
            synthesis.join(timeout=1.0)
            shutdown.join(timeout=1.0)

        assert not synthesis.is_alive()
        assert not shutdown.is_alive()
        assert shutdown_result == [True]

    def test_shutdown_timeout_defers_close_until_request_finishes(self, caplog):
        from app.tts.voicevox_client import VoicevoxClient

        voicevox_client = VoicevoxClient(
            "http://voicevox.local:50021",
            shutdown_drain_seconds=0,
        )
        entered = threading.Event()
        release = threading.Event()

        def blocking_post(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=1.0)
            return _json_response({})

        with (
            patch.object(voicevox_client._client, "post", side_effect=blocking_post),
            patch.object(voicevox_client._client, "close") as close,
        ):
            synthesis = threading.Thread(
                target=lambda: voicevox_client.synthesize("こんにちは", 14)
            )
            synthesis.start()
            assert entered.wait(timeout=1.0)

            assert voicevox_client.close() is False
            assert "outcome=shutdown_timeout" in caplog.text
            close.assert_not_called()
            from app.tts.speech_synthesizer import SpeechSynthesisError

            with pytest.raises(
                SpeechSynthesisError, match="VOICEVOX client is shutting down"
            ):
                voicevox_client.synthesize("次の要求", 14)

            release.set()
            synthesis.join(timeout=1.0)

        assert not synthesis.is_alive()
        close.assert_called_once_with()

    def test_timeout_factory_is_not_public_api(self):
        import app.tts.voicevox_client as voicevox_client

        assert not hasattr(voicevox_client, "_client")
        assert not hasattr(voicevox_client, "voicevox_timeout")
        assert not hasattr(voicevox_client, "_voicevox_timeout")

    def test_wraps_http_status_error_before_synthesis_when_audio_query_fails(
        self, caplog,
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
        caplog.set_level(logging.INFO, logger="app.tts.voicevox_client")
        with patch.object(
            voicevox_client._client,
            "post",
            return_value=failed_response,
        ) as mock_post:
            with pytest.raises(SpeechSynthesisError, match="VOICEVOX request failed"):
                voicevox_client.synthesize("こんにちは", 14)

        assert mock_post.call_count == 1
        failed_response.json.assert_not_called()
        assert "outcome=request_failed" in caplog.text
        assert "こんにちは" not in caplog.text

    @pytest.mark.parametrize(
        ("error", "expected_outcome"),
        [
            (httpx.ConnectError("connection failed"), "connection_failed"),
            (httpx.ReadTimeout("request timed out"), "request_timeout"),
        ],
    )
    def test_logs_connection_and_timeout_without_synthesis_text(
        self, caplog, error, expected_outcome
    ):
        from app.tts.speech_synthesizer import SpeechSynthesisError
        from app.tts.voicevox_client import create_voicevox_client

        voicevox_client = create_voicevox_client("http://voicevox.local:50021")
        caplog.set_level(logging.INFO, logger="app.tts.voicevox_client")
        with patch.object(voicevox_client._client, "post", side_effect=error):
            with pytest.raises(SpeechSynthesisError, match="VOICEVOX request failed"):
                voicevox_client.synthesize("記録してはいけない本文", 14)

        assert f"outcome={expected_outcome}" in caplog.text
        assert "記録してはいけない本文" not in caplog.text

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
