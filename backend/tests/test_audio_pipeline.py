import pytest


class _StubTranscriber:
    def __init__(self, message: str = "音声入力") -> None:
        self.message = message
        self.calls = []

    def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        return self.message


class _StubVoicevoxClient:
    def __init__(self) -> None:
        self.synthesize_calls = []
        self.close_called = False

    def synthesize(self, reply: str, speaker_id: int) -> bytes:
        self.synthesize_calls.append((reply, speaker_id))
        return b"RIFF synthesized"

    def close(self) -> None:
        self.close_called = True


class TestAudioPipelineService:
    def test_wraps_os_error_from_tts_config_as_config_error(self, monkeypatch):
        import app.audio_pipeline as audio_pipeline

        service = audio_pipeline.AudioPipelineService(
            _StubTranscriber(),
            _StubVoicevoxClient(),
        )
        monkeypatch.setattr(
            audio_pipeline,
            "load_tts_config",
            lambda character: (_ for _ in ()).throw(OSError("read failed")),
        )

        with pytest.raises(audio_pipeline.AudioPipelineConfigError) as exc_info:
            service.create_session("miori")

        assert str(exc_info.value) == "character card is not readable"

    def test_wraps_unicode_decode_error_from_tts_config_as_config_error(
        self,
        monkeypatch,
    ):
        import app.audio_pipeline as audio_pipeline

        service = audio_pipeline.AudioPipelineService(
            _StubTranscriber(),
            _StubVoicevoxClient(),
        )
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        monkeypatch.setattr(
            audio_pipeline,
            "load_tts_config",
            lambda character: (_ for _ in ()).throw(error),
        )

        with pytest.raises(audio_pipeline.AudioPipelineConfigError) as exc_info:
            service.create_session("miori")

        assert str(exc_info.value) == "character card is not readable"

    def test_preserves_key_error_message_from_tts_config(self, monkeypatch):
        import app.audio_pipeline as audio_pipeline

        message = "'data' field is missing in character card"
        service = audio_pipeline.AudioPipelineService(
            _StubTranscriber(),
            _StubVoicevoxClient(),
        )
        monkeypatch.setattr(
            audio_pipeline,
            "load_tts_config",
            lambda character: (_ for _ in ()).throw(KeyError(message)),
        )

        with pytest.raises(audio_pipeline.AudioPipelineConfigError) as exc_info:
            service.create_session("miori")

        assert str(exc_info.value) == message

    def test_close_closes_owned_voicevox_client(self):
        import app.audio_pipeline as audio_pipeline

        voicevox_client = _StubVoicevoxClient()
        service = audio_pipeline.AudioPipelineService(
            _StubTranscriber(),
            voicevox_client,
        )

        service.close()

        assert voicevox_client.close_called is True


class TestAudioPipelineSession:
    def test_maps_invalid_pcm16_audio_to_client_input_step_error(self):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        transcriber = _StubTranscriber()

        session = audio_pipeline.AudioPipelineSession(
            tts_config=VoicevoxTtsConfig(speaker_id=14),
            transcriber=transcriber,
            speech_synthesizer=_StubVoicevoxClient(),
        )

        with pytest.raises(audio_pipeline.AudioPipelineStepError) as exc_info:
            session.generate_response_audio(b"\x01\x00\x03", lambda message: "応答")

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == "Audio length must be a multiple of 2 bytes, got 3"
        assert transcriber.calls == []

    def test_wraps_unexpected_stt_errors_as_upstream_step_error(self):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        class FailingTranscriber:
            def transcribe(self, audio: bytes) -> str:
                raise ValueError("model rejected audio")

        session = audio_pipeline.AudioPipelineSession(
            tts_config=VoicevoxTtsConfig(speaker_id=14),
            transcriber=FailingTranscriber(),
            speech_synthesizer=_StubVoicevoxClient(),
        )

        with pytest.raises(audio_pipeline.AudioPipelineStepError) as exc_info:
            session.generate_response_audio(b"\x01\x00", lambda message: "応答")

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail == "STT request failed"

    def test_wraps_unexpected_tts_errors_as_upstream_step_error(self):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        class FailingVoicevoxClient(_StubVoicevoxClient):
            def synthesize(self, reply: str, speaker_id: int) -> bytes:
                raise ValueError("invalid voicevox response")

        session = audio_pipeline.AudioPipelineSession(
            tts_config=VoicevoxTtsConfig(speaker_id=14),
            transcriber=_StubTranscriber(),
            speech_synthesizer=FailingVoicevoxClient(),
        )

        with pytest.raises(audio_pipeline.AudioPipelineStepError) as exc_info:
            session.generate_response_audio(b"\x01\x00", lambda message: "応答")

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail == "VOICEVOX request failed"

    def test_does_not_wrap_llm_reply_generator_errors(self):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        def failing_reply_generator(message: str) -> str:
            raise RuntimeError("llm failed")

        voicevox_client = _StubVoicevoxClient()
        session = audio_pipeline.AudioPipelineSession(
            tts_config=VoicevoxTtsConfig(speaker_id=14),
            transcriber=_StubTranscriber(),
            speech_synthesizer=voicevox_client,
        )

        with pytest.raises(RuntimeError, match="llm failed"):
            session.generate_response_audio(b"\x01\x00", failing_reply_generator)

        assert voicevox_client.synthesize_calls == []
