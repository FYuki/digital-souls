from fastapi.testclient import TestClient
import pytest


def test_lifespan_starts_and_collects_the_single_memory_index_scheduler(
    monkeypatch,
) -> None:
    from app import main

    class RecordingScheduler:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        def start(self) -> None:
            assert not self.started
            self.started = True

        async def stop(self) -> None:
            assert self.started
            self.stopped = True

    scheduler = RecordingScheduler()
    syncs = []

    def build_scheduler(sync):
        syncs.append(sync)
        return scheduler

    monkeypatch.setattr(main, "MemoryIndexScheduler", build_scheduler)

    with TestClient(main.app):
        assert scheduler.started
        assert not scheduler.stopped
        assert len(syncs) == 1
        repository_root = main.Path(main.__file__).resolve().parents[2]
        runtime_paths = main.resolve_runtime_paths(main.os.environ, repository_root)
        assert syncs[0]._chroma_path == runtime_paths.chroma_path
        assert syncs[0]._runtime_report_dir == runtime_paths.runtime_report_dir

    assert scheduler.stopped


def test_lifespan_starts_consolidation_after_index_and_stops_it_first(
    monkeypatch,
) -> None:
    from app import main

    events: list[str] = []

    class RecordingIndexScheduler:
        def __init__(self, _sync) -> None:
            assert _sync is not None

        def start(self) -> None:
            events.append("index:start")

        async def stop(self) -> None:
            events.append("index:stop")

    class RecordingConsolidationScheduler:
        def __init__(self, *_args, **_kwargs) -> None:
            assert _args or _kwargs

        async def start(self) -> None:
            events.append("consolidation:start")

        async def stop(self) -> None:
            events.append("consolidation:stop")

    monkeypatch.setattr(main, "MemoryIndexScheduler", RecordingIndexScheduler)
    monkeypatch.setattr(
        main,
        "MemoryConsolidationScheduler",
        RecordingConsolidationScheduler,
    )

    with TestClient(main.app):
        assert events[-2:] == ["index:start", "consolidation:start"]

    assert events[-2:] == ["consolidation:stop", "index:stop"]


def test_lifespan_wires_a_dedicated_local_client_for_consolidation(
    monkeypatch,
) -> None:
    from app import main

    extractor_clients: list[tuple[str, str | None]] = []
    classifier_clients: list[tuple[str, str | None]] = []
    closed_extractors: list[tuple[str, str | None]] = []
    closed_classifiers: list[tuple[str, str | None]] = []
    semantic_classifiers: list[object] = []
    consolidation_review_classifiers: list[object] = []

    class RecordingExtractorClient:
        def __init__(self, *, model_id: str, base_url: str | None = None) -> None:
            self.model_id = model_id
            self.base_url = base_url
            extractor_clients.append((model_id, base_url))

        def close(self) -> None:
            closed_extractors.append((self.model_id, self.base_url))

    class RecordingClassifierClient:
        def __init__(self, *, model_id: str, base_url: str | None = None) -> None:
            self.model_id = model_id
            self.base_url = base_url
            classifier_clients.append((model_id, base_url))

        def resolve_model_digest(self, *, timeout_seconds: float) -> str:
            assert timeout_seconds > 0
            return "sha256:synthetic"

        def close(self) -> None:
            closed_classifiers.append((self.model_id, self.base_url))

    class RecordingSemanticClassifier:
        def __init__(self, *, client: object, **_kwargs: object) -> None:
            self.client = client
            semantic_classifiers.append(self)

    class RecordingPrivacyReviewer:
        def __init__(self, *, classifier: object, **_kwargs: object) -> None:
            consolidation_review_classifiers.append(classifier)

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/")
    monkeypatch.setattr(
        main,
        "OllamaMemoryExtractorClient",
        RecordingExtractorClient,
    )
    monkeypatch.setattr(main, "OllamaClassifierClient", RecordingClassifierClient)
    monkeypatch.setattr(
        main,
        "OllamaSemanticPrivacyClassifier",
        RecordingSemanticClassifier,
    )
    monkeypatch.setattr(
        main,
        "ConsolidationPrivacyReviewer",
        RecordingPrivacyReviewer,
    )

    with TestClient(main.app):
        pass

    assert [base_url for _, base_url in extractor_clients] == [
        None,
        "http://127.0.0.1:11434",
    ]
    assert [base_url for _, base_url in classifier_clients] == [
        None,
        "http://127.0.0.1:11434",
    ]
    assert len(semantic_classifiers) == 2
    assert consolidation_review_classifiers == [semantic_classifiers[1]]
    assert set(closed_extractors) == set(extractor_clients)
    assert set(closed_classifiers) == set(classifier_clients)


def test_lifespan_rejects_external_consolidation_url_before_client_request(
    monkeypatch,
) -> None:
    from app import main

    requests: list[object] = []

    class RecordingClient:
        def __init__(self, *, model_id: str, base_url: str | None = None) -> None:
            self.model_id = model_id
            self.base_url = base_url

        def chat(self, *args: object, **kwargs: object) -> str:
            requests.append((args, kwargs))
            return ""

        def close(self) -> None:
            pass

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(main, "OllamaMemoryExtractorClient", RecordingClient)
    monkeypatch.setattr(main, "OllamaClassifierClient", RecordingClient)

    with pytest.raises(ValueError, match="loopback host"):
        with TestClient(main.app):
            pass

    assert requests == []
