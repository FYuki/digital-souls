import inspect
from types import SimpleNamespace

import pytest
from app.chat_service import ChatReply, PersistedPrivacySkippedTurn
from app.privacy.contracts import HistoryDecisionReasonCode
from tests.chat_reply_test_support import persisted_reply
from tests.conversation_history_test_support import TURN_ID


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
    def test_wraps_permission_error_from_tts_config_as_config_error(self, monkeypatch):
        import app.audio_pipeline as audio_pipeline

        service = audio_pipeline.AudioPipelineService(
            _StubTranscriber(),
            _StubVoicevoxClient(),
        )
        monkeypatch.setattr(
            audio_pipeline,
            "load_tts_config",
            lambda character: (_ for _ in ()).throw(PermissionError("denied")),
        )

        with pytest.raises(audio_pipeline.AudioPipelineConfigError) as exc_info:
            service.create_session("miori")

        assert str(exc_info.value) == "character card is not readable"

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
    def test_records_privacy_skip_as_an_excluded_response(self):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        recorded = []
        measurement = SimpleNamespace(
            measurement_kind="automated_test",
            session_id="session-1",
            utterance_id="utterance-1",
            response_id="response-1",
            record=recorded.append,
            clock_ns=iter(range(1_000_000, 20_000_000, 1_000_000)).__next__,
        )
        privacy_turn = PersistedPrivacySkippedTurn(
            turn_id=TURN_ID,
            reason_code=HistoryDecisionReasonCode.STORAGE_OPT_OUT,
            sanitizer_version="sanitizer-v1",
            policy_version="policy-v1",
        )
        session = audio_pipeline.AudioPipelineSession(
            tts_config=VoicevoxTtsConfig(speaker_id=14),
            transcriber=_StubTranscriber(),
            speech_synthesizer=_StubVoicevoxClient(),
            measurement=measurement,
        )

        _message, _reply, audio = session.generate_response_audio(
            b"\x01\x00",
            lambda _message: ChatReply(turn_id=TURN_ID, persisted_turn=privacy_turn),
        )

        assert audio == b""
        excluded = recorded[-1]
        assert excluded.outcome == "excluded"
        assert excluded.reason_code == "privacy_skip"
        assert (excluded.session_id, excluded.utterance_id, excluded.response_id) == (
            "session-1",
            "utterance-1",
            "response-1",
        )

    def test_records_correlated_stt_llm_and_tts_stage_events(self):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        recorded = []
        measurement = SimpleNamespace(
            measurement_kind="automated_test",
            session_id="session-1",
            utterance_id="utterance-1",
            response_id="response-1",
            record=recorded.append,
            clock_ns=iter(range(1_000_000, 20_000_000, 1_000_000)).__next__,
        )
        assert "measurement" in inspect.signature(
            audio_pipeline.AudioPipelineSession
        ).parameters, "AudioPipelineSessionに計測依存の注入経路がありません"
        session = audio_pipeline.AudioPipelineSession(
            tts_config=VoicevoxTtsConfig(speaker_id=14),
            transcriber=_StubTranscriber("今日の音声"),
            speech_synthesizer=_StubVoicevoxClient(),
            measurement=measurement,
        )

        session.generate_response_audio(
            b"\x01\x00",
            lambda message: persisted_reply(f"応答:{message}", TURN_ID),
        )

        assert [event.name for event in recorded] == [
            "stt_started",
            "stt_completed",
            "llm_started",
            "first_text_delta",
            "llm_completed",
            "tts_started",
            "tts_completed",
        ]
        assert all(event.session_id == "session-1" for event in recorded)
        assert all(event.utterance_id == "utterance-1" for event in recorded)
        assert all(event.response_id == "response-1" for event in recorded)

    def test_records_a_safe_failed_stage_outcome(self):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        class FailingTranscriber:
            def transcribe(self, audio: bytes) -> str:
                raise ValueError("SECRET_TRANSCRIBER_DETAIL_07D1")

        recorded = []
        measurement = SimpleNamespace(
            measurement_kind="automated_test",
            session_id="session-1",
            utterance_id="utterance-1",
            response_id="response-1",
            record=recorded.append,
            clock_ns=iter(range(1_000_000, 20_000_000, 1_000_000)).__next__,
        )
        if "measurement" not in inspect.signature(
            audio_pipeline.AudioPipelineSession
        ).parameters:
            pytest.skip("計測依存の注入経路が実装された後にfailure outcomeを検証します")
        session = audio_pipeline.AudioPipelineSession(
            tts_config=VoicevoxTtsConfig(speaker_id=14),
            transcriber=FailingTranscriber(),
            speech_synthesizer=_StubVoicevoxClient(),
            measurement=measurement,
        )

        with pytest.raises(audio_pipeline.AudioPipelineStepError):
            session.generate_response_audio(
                b"\x01\x00",
                lambda message: persisted_reply("応答", TURN_ID),
            )

        assert [event.name for event in recorded] == ["stt_started", "stt_failed"]
        assert recorded[-1].outcome == "failure"
        assert recorded[-1].reason_code == "stt_upstream_failed"
        assert "SECRET_TRANSCRIBER_DETAIL_07D1" not in str(recorded[-1])

    def test_returns_transcript_reply_and_audio(self):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        voicevox_client = _StubVoicevoxClient()
        session = audio_pipeline.AudioPipelineSession(
            tts_config=VoicevoxTtsConfig(speaker_id=14),
            transcriber=_StubTranscriber("今日の音声"),
            speech_synthesizer=voicevox_client,
        )

        transcript, reply, audio = session.generate_response_audio(
            b"\x01\x00",
            lambda message: persisted_reply(f"応答:{message}", TURN_ID),
        )

        assert transcript == "今日の音声"
        assert reply == persisted_reply("応答:今日の音声", TURN_ID)
        assert audio == b"RIFF synthesized"
        assert voicevox_client.synthesize_calls == [("応答:今日の音声", 14)]

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
            session.generate_response_audio(
                b"\x01\x00",
                lambda message: persisted_reply("応答", TURN_ID),
            )

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
            session.generate_response_audio(
                b"\x01\x00",
                lambda message: persisted_reply("応答", TURN_ID),
            )

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail == "VOICEVOX request failed"

    def test_does_not_wrap_llm_reply_generator_errors(self):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        def failing_reply_generator(message: str) -> ChatReply:
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
