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
    monkeypatch.setattr(main, "MemoryIndexScheduler", lambda *_args, **_kwargs: scheduler)

    with TestClient(main.app):
        assert scheduler.started
        assert not scheduler.stopped

    assert scheduler.stopped
