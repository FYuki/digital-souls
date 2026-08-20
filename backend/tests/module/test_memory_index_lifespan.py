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
