from fastapi.testclient import TestClient


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


def test_lifespan_wires_fixed_inference_callers_for_memory_generation(
    monkeypatch,
) -> None:
    from app import main
    from app.inference import InferenceCaller, InferenceTarget

    clients: list[tuple[InferenceCaller, InferenceTarget]] = []
    closed: list[tuple[InferenceCaller, InferenceTarget]] = []

    class RecordingInferenceClient:
        def __init__(
            self,
            *,
            caller: InferenceCaller,
            target: InferenceTarget,
            **_kwargs: object,
        ) -> None:
            self.identity = (caller, target)
            clients.append(self.identity)

        def close(self) -> None:
            closed.append(self.identity)

    monkeypatch.setattr(
        main,
        "StructuredMemoryInferenceClient",
        RecordingInferenceClient,
    )

    with TestClient(main.app):
        pass

    assert clients == [
        (InferenceCaller.MEMORY_EXTRACTION, InferenceTarget.MEMORY_EXTRACTION),
        (
            InferenceCaller.MEMORY_CONSOLIDATION,
            InferenceTarget.MEMORY_CONSOLIDATION,
        ),
    ]
    assert closed == list(reversed(clients))


def test_lifespan_does_not_apply_ollama_endpoint_policy_in_memory_domain(
    monkeypatch,
) -> None:
    from app import main

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")

    with TestClient(main.app):
        assert main.app.state.inference_router is not None
