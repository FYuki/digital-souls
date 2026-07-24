import inspect
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


_BACKEND_DIR = Path(__file__).parent.parent.parent


class TestRuntimeConfiguration:
    def test_repository_has_no_unmerged_index_entries(self):
        result = subprocess.run(
            ["git", "ls-files", "-u"],
            cwd=_BACKEND_DIR.parent,
            check=True,
            capture_output=True,
            text=True,
        )

        assert result.stdout == ""

    def test_repository_status_has_no_unmerged_conflict_codes(self):
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=_BACKEND_DIR.parent,
            check=True,
            capture_output=True,
            text=True,
        )
        conflict_lines = [
            line
            for line in result.stdout.splitlines()
            if line.startswith(("AA ", "UU "))
        ]

        assert conflict_lines == []

    def test_runtime_requirements_include_fastapi_backend_dependencies(self):
        required = {"fastapi", "uvicorn[standard]", "httpx", "python-dotenv", "chromadb"}

        lines = (_BACKEND_DIR / "requirements.txt").read_text().splitlines()
        packages = {
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        }

        assert required.issubset(packages)

    def test_env_example_declares_ollama_base_url(self):
        lines = (_BACKEND_DIR / ".env.example").read_text().splitlines()

        assert "OLLAMA_BASE_URL=http://localhost:11434" in lines

    def test_env_example_declares_ollama_embedding_model(self):
        lines = (_BACKEND_DIR / ".env.example").read_text().splitlines()

        assert "OLLAMA_EMBEDDING_MODEL=nomic-embed-text:latest" in lines

    def test_env_example_declares_rag_enabled_switch(self):
        lines = (_BACKEND_DIR / ".env.example").read_text().splitlines()

        assert "RAG_ENABLED=false" in lines

    def test_rag_enabled_is_not_resolved_inside_memory_service(self):
        import app.memory.rag_service as rag_service

        source = inspect.getsource(rag_service)

        assert "os.environ" not in source
        assert "RAG_ENABLED" not in source

    def test_memory_policy_is_resolved_at_chat_boundary_only(self):
        import app._chat_runtime as chat_runtime
        import app.memory.rag_service as rag_service
        import app.chat_service as chat_service
        import app.main as main

        rag_source = inspect.getsource(rag_service)
        chat_runtime_source = inspect.getsource(chat_runtime)
        chat_service_source = inspect.getsource(chat_service)
        main_source = inspect.getsource(main)

        assert "resolved_memory_policy" not in rag_source
        assert "resolved_memory_policy" not in chat_service_source
        assert "resolved_memory_policy" in chat_runtime_source
        assert "resolve_chat_runtime_config" in main_source

    def test_chat_service_public_api_exposes_only_chat_entrypoints_without_rag_queue(self):
        import app.chat_service as chat_service

        source = inspect.getsource(chat_service)

        assert "os.environ" not in source
        assert "_DEFAULT_MEMORY_TASK_QUEUE" not in source
        assert "memory_task_queue_scope" not in source
        assert "_ThreadedMemoryTaskQueue" not in source
        assert "_configured_memory_task_queue" not in source
        assert "_queue_lock" not in source
        assert not hasattr(chat_service, "configure_memory_task_queue")
        assert not hasattr(chat_service, "clear_memory_task_queue")
        assert hasattr(chat_service, "generate_chat_reply")
        assert hasattr(chat_service, "create_chat_session")
        assert not hasattr(chat_service, "ChatRuntimeConfig")
        assert not hasattr(chat_service, "ChatService")
        assert not hasattr(chat_service, "ThreadPoolMemoryTaskQueue")
        assert not hasattr(chat_service, "create_chat_service")
        assert "configure_memory_task_queue" not in chat_service.__all__
        assert "clear_memory_task_queue" not in chat_service.__all__
        assert "generate_chat_reply" in chat_service.__all__
        assert "create_chat_session" in chat_service.__all__
        assert "ChatService" not in chat_service.__all__
        assert "ThreadPoolMemoryTaskQueue" not in chat_service.__all__
        assert "create_chat_service" not in chat_service.__all__
        assert "Thread(" not in source
        assert "RuntimeError" not in source

    def test_chat_routes_use_single_chat_service_entrypoints(self):
        import app.chat_service as chat_service
        import app.routers.chat as chat_router
        import app.routers.ws as ws_router

        chat_service_source = inspect.getsource(chat_service)
        chat_router_source = inspect.getsource(chat_router)
        ws_router_source = inspect.getsource(ws_router)

        assert "_generate_chat_reply_for_runtime" not in chat_service_source
        assert "_create_chat_session_for_runtime" not in chat_service_source
        assert "_generate_chat_reply_with_memory_queue" not in chat_service_source
        assert "_create_chat_session_with_memory_queue" not in chat_service_source
        assert "_generate_chat_reply_with_memory_queue" not in chat_router_source
        assert "_create_chat_session_with_memory_queue" not in ws_router_source
        assert "memory_task_queue_scope" not in chat_router_source
        assert "memory_task_queue_scope" not in ws_router_source
        assert "app.chat_runtime" not in chat_router_source
        assert "app.chat_runtime" not in ws_router_source
        assert "request.app.state.chat_service" in chat_router_source
        assert "websocket.app.state.chat_service" in ws_router_source
        assert "generate_chat_reply(" in chat_router_source
        assert "create_chat_session(" in ws_router_source

    def test_ollama_environment_is_resolved_in_llm_boundary_only(self):
        import app.memory.embedder as embedder

        source = inspect.getsource(embedder)

        assert "os.environ" not in source
        assert "OLLAMA_BASE_URL" not in source
        assert "OLLAMA_EMBEDDING_MODEL" not in source

    def test_memory_policy_config_declares_common_and_service_sections(self):
        import json

        config_path = _BACKEND_DIR / "app" / "memory" / "memory_policy.json"

        config = json.loads(config_path.read_text(encoding="utf-8"))

        assert set(config) == {"common", "services"}
        assert set(config["common"]) == {
            "sensitive_terms",
            "do_not_store_terms",
            "explicit_memory_terms",
            "long_term_memory_markers",
        }
        assert isinstance(config["services"], dict)
        assert "rag_service" in config["services"]
        assert isinstance(config["services"]["rag_service"], dict)
        assert "max_retrieved_memories" in config["services"]["rag_service"]
        assert "characters" not in config

    def test_memory_policy_module_does_not_expose_test_only_persistence_helper(self):
        import app.memory.memory_policy as memory_policy

        assert not hasattr(memory_policy, "can_persist_memory")

    def test_memory_policy_does_not_expose_raw_service_sections(self):
        import app.memory.memory_policy as memory_policy

        policy = memory_policy.resolved_memory_policy()

        assert not hasattr(policy, "services")

    def test_chat_and_ws_routes_delegate_to_same_app_chat_service(self):
        import app.main as main

        class StubSession:
            def __init__(self, service):
                self._service = service

            def generate_reply(self, message: str) -> str:
                self._service.calls.append(("ws-reply", "miori", message))
                return f"ws:{message}"

        class StubChatService:
            def __init__(self):
                self.calls = []

            def generate_chat_reply(self, character: str, message: str) -> str:
                self.calls.append(("http", character, message))
                return f"http:{message}"

            async def create_chat_session(self, character: str):
                self.calls.append(("ws-open", character))
                return StubSession(self)

        stub_service = StubChatService()
        with TestClient(main.app) as client:
            main.app.state.chat_service = stub_service
            http_response = client.post(
                "/chat",
                json={"character": "miori", "message": "hello"},
            )
            with client.websocket_connect("/ws/miori") as websocket:
                websocket.send_json({"type": "text", "message": "hello"})
                ws_response = websocket.receive_json()

        assert http_response.status_code == 200
        assert http_response.json() == {"character": "miori", "response": "http:hello"}
        assert ws_response == {"type": "text", "response": "ws:hello"}
        assert stub_service.calls == [
            ("http", "miori", "hello"),
            ("ws-open", "miori"),
            ("ws-reply", "miori", "hello"),
        ]

    def test_ws_route_uses_app_audio_pipeline_service_for_audio_frames(self):
        import app.main as main

        class StubChatSession:
            def __init__(self) -> None:
                self.messages = []

            def generate_reply(self, message: str) -> str:
                self.messages.append(message)
                return f"reply:{message}"

        class StubChatService:
            async def create_chat_session(self, character: str) -> StubChatSession:
                return StubChatSession()

        class StubAudioSession:
            def __init__(self) -> None:
                self.calls = []

            def generate_response_audio(self, audio: bytes, reply_generator):
                reply = reply_generator("transcribed")
                self.calls.append((audio, reply))
                return "transcribed", reply, b"RIFF delegated"

        class StubAudioPipelineService:
            def __init__(self) -> None:
                self.sessions = []

            def create_session(self, character: str) -> StubAudioSession:
                session = StubAudioSession()
                self.sessions.append((character, session))
                return session

            def close(self) -> None:
                return None

        audio_service = StubAudioPipelineService()
        with TestClient(main.app) as client:
            main.app.state.chat_service = StubChatService()
            main.app.state.audio_pipeline_service = audio_service
            with client.websocket_connect("/ws/miori") as websocket:
                websocket.send_bytes(b"\x01\x00")
                user_text = websocket.receive_json()
                miori_text = websocket.receive_json()
                response = websocket.receive_bytes()

        assert user_text == {"type": "text", "speaker": "user", "message": "transcribed"}
        assert miori_text == {
            "type": "text",
            "speaker": "miori",
            "response": "reply:transcribed",
        }
        assert response == b"RIFF delegated"
        assert len(audio_service.sessions) == 1
        character, session = audio_service.sessions[0]
        assert character == "miori"
        assert session.calls == [(b"\x01\x00", "reply:transcribed")]

    def test_audio_pipeline_session_runs_audio_steps_and_logs_latency(
        self,
        caplog,
    ):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        class StubTranscriber:
            def __init__(self) -> None:
                self.calls = []

            def transcribe(self, audio: bytes) -> str:
                self.calls.append(audio)
                return "音声入力"

        class StubVoicevoxClient:
            def __init__(self) -> None:
                self.synthesize_calls = []

            def synthesize(
                self,
                reply: str,
                speaker_id: int,
            ) -> bytes:
                self.synthesize_calls.append((reply, speaker_id))
                return b"RIFF synthesized"

        transcriber = StubTranscriber()
        voicevox_client = StubVoicevoxClient()
        session = audio_pipeline.AudioPipelineSession(
            tts_config=VoicevoxTtsConfig(speaker_id=14),
            transcriber=transcriber,
            speech_synthesizer=voicevox_client,
        )

        with caplog.at_level("INFO", logger="app.audio_pipeline"):
            transcript, reply, audio = session.generate_response_audio(
                b"\x01\x00",
                lambda message: f"応答:{message}",
            )

        assert transcript == "音声入力"
        assert reply == "応答:音声入力"
        assert audio == b"RIFF synthesized"
        assert transcriber.calls == [b"\x01\x00"]
        assert voicevox_client.synthesize_calls == [
            ("応答:音声入力", 14)
        ]
        messages = [record.getMessage() for record in caplog.records]
        assert any("STT completed in" in message for message in messages)
        assert any("LLM completed in" in message for message in messages)
        assert any("VOICEVOX completed in" in message for message in messages)

    def test_audio_pipeline_service_accepts_speech_synthesizer_protocol(self):
        import app.audio_pipeline as audio_pipeline

        signature = inspect.signature(audio_pipeline.AudioPipelineService)
        annotation = signature.parameters["speech_synthesizer"].annotation
        synthesize_signature = inspect.signature(audio_pipeline.SpeechSynthesizer.synthesize)

        assert annotation is audio_pipeline.SpeechSynthesizer
        assert list(synthesize_signature.parameters) == ["self", "text", "speaker_id"]
        assert not hasattr(audio_pipeline, "VoicevoxClient")

    def test_audio_pipeline_service_accepts_speech_transcriber_protocol(self):
        import app.audio_pipeline as audio_pipeline

        service_signature = inspect.signature(audio_pipeline.AudioPipelineService)
        service_annotation = service_signature.parameters["transcriber"].annotation

        assert service_annotation is audio_pipeline.SpeechTranscriber
        assert not hasattr(audio_pipeline, "WhisperTranscriber")

    def test_audio_pipeline_session_public_api_hides_collaborators(self):
        import app.audio_pipeline as audio_pipeline
        from app.characters.loader import VoicevoxTtsConfig

        class StubTranscriber:
            def transcribe(self, audio: bytes) -> str:
                return "音声入力"

        class StubSpeechSynthesizer:
            def synthesize(self, reply: str, speaker_id: int) -> bytes:
                return b"RIFF synthesized"

            def close(self) -> None:
                pass

        session = audio_pipeline.AudioPipelineSession(
            tts_config=VoicevoxTtsConfig(speaker_id=14),
            transcriber=StubTranscriber(),
            speech_synthesizer=StubSpeechSynthesizer(),
        )

        assert list(
            name for name in dir(session) if not name.startswith("_")
        ) == ["generate_response_audio"]
        assert not hasattr(audio_pipeline, "AudioPipelineResponse")
        assert not hasattr(audio_pipeline, "_AudioPipelineResponse")
        assert inspect.signature(session.generate_response_audio).return_annotation == tuple[
            str, str, bytes
        ]

    def test_audio_pipeline_does_not_depend_on_httpx_transport_errors(self):
        import app.audio_pipeline as audio_pipeline

        source = inspect.getsource(audio_pipeline)

        assert "import httpx" not in source
        assert "httpx.HTTPError" not in source

    def test_main_lifespan_owns_audio_pipeline_service_state(self):
        import app.main as main

        assert not hasattr(main.app.state, "audio_pipeline_service")

    def test_main_lifespan_closes_audio_pipeline_service(self, monkeypatch):
        import app.main as main

        class StubAudioPipelineService:
            def __init__(self) -> None:
                self.close_called = False

            def close(self) -> None:
                self.close_called = True

        audio_service = StubAudioPipelineService()

        def create_audio_pipeline_service_stub(_runtime_config):
            return audio_service

        monkeypatch.setattr(
            main,
            "create_audio_pipeline_service",
            create_audio_pipeline_service_stub,
        )

        with TestClient(main.app):
            assert audio_service.close_called is False

        assert audio_service.close_called is True
        assert not hasattr(main.app.state, "audio_pipeline_service")
        with TestClient(main.app):
            assert hasattr(main.app.state, "audio_pipeline_service")
        assert not hasattr(main.app.state, "audio_pipeline_service")

    def test_main_lifespan_completes_cleanup_when_audio_close_fails(self, monkeypatch):
        import app.chat_service as chat_service
        import app.main as main

        class FailingAudioPipelineService:
            def close(self) -> None:
                raise RuntimeError("audio close failed")

        class RecordingExecutor:
            def __init__(self, *args, **kwargs):
                self.shutdown_called = False

            def shutdown(self, wait: bool) -> None:
                self.shutdown_called = wait

        executor = RecordingExecutor()
        monkeypatch.setattr(
            main,
            "create_audio_pipeline_service",
            lambda _runtime_config: FailingAudioPipelineService(),
        )
        monkeypatch.setattr(
            main,
            "ThreadPoolExecutor",
            lambda *args, **kwargs: executor,
        )

        with pytest.raises(RuntimeError, match="audio close failed"):
            with TestClient(main.app):
                assert hasattr(main.app.state, "audio_pipeline_service")

        assert not hasattr(main.app.state, "conversation_history_repository")
        assert not hasattr(main.app.state, "chat_service")
        assert not hasattr(main.app.state, "audio_pipeline_service")
        assert executor.shutdown_called is True
        with pytest.raises(chat_service.ChatServiceError):
            chat_service.generate_chat_reply("miori", "hello")

    def test_main_lifespan_shuts_down_chat_service_task_queue_executor(self, monkeypatch):
        import app.main as main

        class RecordingExecutor:
            def __init__(self, *args, **kwargs):
                self.shutdown_called = False

            def shutdown(self, wait: bool) -> None:
                self.shutdown_called = wait

        executor = RecordingExecutor()

        def executor_factory(*args, **kwargs):
            return executor

        monkeypatch.setattr(main, "ThreadPoolExecutor", executor_factory)

        with TestClient(main.app):
            assert executor.shutdown_called is False

        assert executor.shutdown_called is True

    def test_main_lifespan_cleans_runtime_when_config_resolution_fails(self, monkeypatch):
        import app.chat_service as chat_service
        import app.main as main

        class RecordingExecutor:
            def __init__(self, *args, **kwargs):
                self.shutdown_called = False

            def shutdown(self, wait: bool) -> None:
                self.shutdown_called = wait

        executor = RecordingExecutor()

        def executor_factory(*args, **kwargs):
            return executor

        def fail_config_resolution():
            raise ValueError("invalid memory policy")

        monkeypatch.setattr(main, "ThreadPoolExecutor", executor_factory)
        monkeypatch.setattr(
            main._chat_runtime,
            "resolve_chat_runtime_config",
            fail_config_resolution,
        )

        with pytest.raises(ValueError, match="invalid memory policy"):
            with TestClient(main.app):
                raise AssertionError("startup should fail before yielding")

        assert executor.shutdown_called is True
        assert not hasattr(main.app.state, "conversation_history_repository")
        assert not hasattr(main.app.state, "chat_service")
        assert not hasattr(main.app.state, "audio_pipeline_service")
        with pytest.raises(chat_service.ChatServiceError):
            chat_service.generate_chat_reply("miori", "hello")

    def test_main_lifespan_owns_chat_service_state_and_cleans_it_up(self):
        import app.main as main

        assert not hasattr(main.app.state, "memory_task_queue")
        assert not hasattr(main.app.state, "chat_runtime")
        assert not hasattr(main.app.state, "chat_service")
        assert not hasattr(main.app.state, "audio_pipeline_service")
        with TestClient(main.app):
            assert not hasattr(main.app.state, "memory_task_queue")
            assert not hasattr(main.app.state, "chat_runtime")
            assert hasattr(main.app.state, "chat_service")
            assert hasattr(main.app.state, "audio_pipeline_service")
        assert not hasattr(main.app.state, "memory_task_queue")
        assert not hasattr(main.app.state, "chat_runtime")
        assert not hasattr(main.app.state, "chat_service")
        assert not hasattr(main.app.state, "audio_pipeline_service")

    def test_main_lifespan_registers_module_entrypoints_to_app_chat_service(self):
        import app.chat_service as chat_service
        import app.main as main

        class StubChatService:
            def generate_chat_reply(self, character: str, message: str) -> str:
                return f"{character}:{message}"

            async def create_chat_session(self, character: str):
                raise AssertionError("not used")

        with TestClient(main.app):
            main.app.state.chat_service = StubChatService()
            assert chat_service.generate_chat_reply("miori", "hello") == "miori:hello"

    def test_memory_modules_do_not_reference_character_memory_policy_markdown(self):
        memory_dir = _BACKEND_DIR / "app" / "memory"

        sources = [
            path.read_text(encoding="utf-8")
            for path in memory_dir.glob("*.py")
            if path.name != "__init__.py"
        ]

        assert all("memory-policy.md" not in source for source in sources)


class TestFastAPIContract:
    def test_main_app_registers_post_chat_route(self):
        from app.main import app

        paths = app.openapi()["paths"]

        assert "post" in paths["/chat"]

    def test_main_app_registers_websocket_chat_route(self):
        from app.main import app

        route_paths = set()
        pending_routes = list(app.routes)
        while pending_routes:
            route = pending_routes.pop()
            path = getattr(route, "path", None)
            if path is not None:
                route_paths.add(path)
            pending_routes.extend(getattr(route, "routes", []))
            original_router = getattr(route, "original_router", None)
            if original_router is not None:
                pending_routes.extend(original_router.routes)

        assert "/ws/{character_name}" in route_paths

    def test_root_health_check_returns_ok_for_backend_probe(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestLLMClientContract:
    def test_base_client_cannot_be_instantiated_without_generate(self):
        from app.llm.base import LLMClient

        with pytest.raises(TypeError):
            LLMClient()

    def test_generate_signature_matches_router_contract(self):
        from app.llm.base import LLMClient

        signature = inspect.signature(LLMClient.generate)

        assert list(signature.parameters) == [
            "self",
            "system_prompt",
            "user_message",
        ]
        assert signature.return_annotation is str
