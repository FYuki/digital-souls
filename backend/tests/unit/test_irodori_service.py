from __future__ import annotations

import asyncio
import io
import os
import threading
import time
import wave
from dataclasses import replace
from multiprocessing.connection import Connection
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from irodori_service.app import create_app
from irodori_service.config import ServiceConfig
from irodori_service.contracts import ServiceError, SpeechRequest
from irodori_service.scheduler import SynthesisScheduler
from irodori_service.voices import RegisteredVoices, register_voice
from irodori_service.worker import ProcessWorker


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48_000)
        wav.writeframes(b"\x10\x00" * 480)
    return output.getvalue()


def payload(text: str = "合成します。") -> SpeechRequest:
    return SpeechRequest(
        input=text, voice="miori-b3-4221",
        irodori={"caption": "穏やかな女性の声", "seed": 4221, "num_steps": 40},
    )


def config(tmp_path: Path, **kwargs) -> ServiceConfig:
    return ServiceConfig(voices_dir=tmp_path / "voices", model_cache=tmp_path / "models", **kwargs)


class BlockingWorker:
    def __init__(self) -> None:
        self.ready = False
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def start(self) -> None:
        self.ready = True

    def synthesize(self, request: SpeechRequest) -> bytes:
        self.calls.append(request.input)
        self.entered.set()
        if len(self.calls) == 1 and not self.release.wait(3):
            raise AssertionError("test worker was not released")
        return wav_bytes()

    def close(self) -> None:
        self.ready = False
        self.release.set()


async def wait_pending(scheduler: SynthesisScheduler, count: int) -> None:
    for _ in range(100):
        if scheduler.pending_count == count:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("pending request count did not converge")


def test_dogfood_waiters_pass_development_without_preempting_active(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker = BlockingWorker()
        scheduler = SynthesisScheduler(worker, config(tmp_path))
        await scheduler.start()
        first = asyncio.create_task(scheduler.submit(payload("実行中"), "dev"))
        assert await asyncio.to_thread(worker.entered.wait, 1)
        second = asyncio.create_task(scheduler.submit(payload("開発待機"), "dev"))
        third = asyncio.create_task(scheduler.submit(payload("運用待機"), "dogfood"))
        await wait_pending(scheduler, 2)
        assert worker.calls == ["実行中"]
        worker.release.set()
        assert await asyncio.gather(first, second, third) == [wav_bytes()] * 3
        assert worker.calls == ["実行中", "運用待機", "開発待機"]
        await scheduler.close()

    asyncio.run(scenario())


def test_cancel_keeps_active_gpu_slot_and_removes_abandoned_waiter(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker = BlockingWorker()
        scheduler = SynthesisScheduler(worker, config(tmp_path))
        await scheduler.start()
        first = asyncio.create_task(scheduler.submit(payload("破棄する実行"), "dev"))
        assert await asyncio.to_thread(worker.entered.wait, 1)
        abandoned = asyncio.create_task(scheduler.submit(payload("破棄する待機"), "dogfood"))
        await wait_pending(scheduler, 1)
        first.cancel()
        abandoned.cancel()
        await asyncio.gather(first, abandoned, return_exceptions=True)
        next_request = asyncio.create_task(scheduler.submit(payload("次の会話"), "dogfood"))
        await wait_pending(scheduler, 1)
        assert scheduler.active_count == 1
        assert worker.calls == ["破棄する実行"]
        worker.release.set()
        assert await next_request == wav_bytes()
        assert worker.calls == ["破棄する実行", "次の会話"]
        await scheduler.close()

    asyncio.run(scenario())


def test_pending_count_and_wait_time_are_bounded(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker = BlockingWorker()
        scheduler = SynthesisScheduler(worker, config(tmp_path, max_pending=1, queue_timeout=0.05))
        await scheduler.start()
        first = asyncio.create_task(scheduler.submit(payload("実行"), "dev"))
        assert await asyncio.to_thread(worker.entered.wait, 1)
        waiting = asyncio.create_task(scheduler.submit(payload("時間切れ"), "dev"))
        await wait_pending(scheduler, 1)
        with pytest.raises(ServiceError, match="tts_capacity_exceeded"):
            await scheduler.submit(payload("超過"), "dogfood")
        with pytest.raises(ServiceError, match="tts_queue_timeout"):
            await waiting
        assert scheduler.pending_count == 0
        worker.release.set()
        await first
        assert worker.calls == ["実行"]
        await scheduler.close()

    asyncio.run(scenario())


def fake_process_worker(connection: Connection, _config: ServiceConfig) -> None:
    connection.send(("ready",))
    try:
        while True:
            request = connection.recv()
            if request["input"] == "停止しない推論":
                time.sleep(30)
            if request["input"] == "返信途中で停止":
                # Connection frameのheaderと本文の先頭だけを送り、残りの転送を停止する。
                os.write(connection.fileno(), b"\x00\x00\x04\x00part")
                time.sleep(30)
            connection.send(("ok", wav_bytes()))
    except (EOFError, OSError):
        pass


def test_timeout_terminates_owned_process_then_can_prepare_again(tmp_path: Path) -> None:
    worker = ProcessWorker(config(tmp_path, inference_timeout=0.05), target=fake_process_worker)
    assert not worker.ready
    try:
        worker.start()
        old_pid = worker._worker.pid
        assert worker.ready and worker.preparation_seconds is not None
        with pytest.raises(ServiceError, match="tts_inference_timeout"):
            worker.synthesize(payload("停止しない推論"))
        assert not worker.ready
        assert worker._worker is None
        worker.start()
        assert worker._worker.pid != old_pid
        assert worker.synthesize(payload()) == wav_bytes()
    finally:
        worker.close()


def register_selected(config: ServiceConfig) -> None:
    assets = Path(__file__).resolve().parents[3] / "characters/miori/assets/voice"
    register_voice(assets / "miori-b3-4221.wav", assets / "miori-b3-4221.json", config.voices_dir)


def test_registration_is_idempotent_but_rejects_modified_registered_assets(tmp_path: Path) -> None:
    settings = config(tmp_path)
    register_selected(settings)
    register_selected(settings)
    voices = RegisteredVoices(settings.voices_dir)
    assert voices.list_ids() == ["miori-b3-4221"]
    original = voices.resolve("miori-b3-4221")
    original.chmod(0o644)
    original.write_bytes(wav_bytes())
    with pytest.raises(ServiceError, match="tts_voice_changed"):
        voices.resolve("miori-b3-4221")
    with pytest.raises(ValueError, match="cannot be overwritten"):
        register_selected(settings)


def test_service_uses_selected_reference_and_rejects_arbitrary_reference_paths(tmp_path: Path) -> None:
    settings = config(tmp_path)
    register_selected(settings)
    worker = BlockingWorker()
    worker.release.set()
    app = create_app(settings, worker)
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/v1/audio/voices").json()["data"][0]["id"] == "miori-b3-4221"
        body = payload().model_dump()
        headers = {"X-DS-Environment": "dogfood"}
        result = client.post("/v1/audio/speech", json=body, headers=headers)
        assert result.status_code == 200
        assert result.content == wav_bytes()
        body["irodori"]["ref_wav"] = "/private/forbidden.wav"
        rejected = client.post("/v1/audio/speech", json=body, headers=headers)
        assert rejected.status_code == 422
        assert "private" not in rejected.text
        assert len(worker.calls) == 1
        assert client.put("/v1/audio/voices/miori-b3-4221").status_code == 404
        assert client.delete("/v1/audio/voices/miori-b3-4221").status_code == 404


def test_not_ready_is_not_inferred_from_liveness_and_errors_hide_input(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(config(tmp_path), BlockingWorker())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 503
            body = payload("保存しない試験本文").model_dump()
            body["voice"] = "none"
            response = await client.post("/v1/audio/speech", json=body, headers={"X-DS-Environment": "test"})
            assert response.status_code == 422
            assert "保存しない" not in response.text
            oversized = await client.post(
                "/v1/audio/speech", content=b"x" * 65537, headers={"X-DS-Environment": "dev"},
            )
            assert oversized.status_code == 413

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1])
def test_non_finite_or_non_positive_limits_are_rejected(tmp_path: Path, value: float) -> None:
    with pytest.raises(ValueError):
        replace(config(tmp_path), inference_timeout=value)

def test_idle_worker_death_is_recovered_before_next_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker = BlockingWorker()
        worker.release.set()
        scheduler = SynthesisScheduler(worker, config(tmp_path))
        await scheduler.start()
        worker.ready = False
        for _ in range(100):
            if scheduler.ready:
                break
            await asyncio.sleep(0.01)
        assert scheduler.ready
        assert await scheduler.submit(payload(), "test") == wav_bytes()
        await scheduler.close()

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="Unix socketの部分frameを再現する")
def test_partial_worker_reply_obeys_inference_deadline_and_allows_restart(tmp_path: Path) -> None:
    worker = ProcessWorker(config(tmp_path, inference_timeout=0.05), target=fake_process_worker)
    outcome: list[BaseException | bytes] = []

    def synthesize() -> None:
        try:
            outcome.append(worker.synthesize(payload("返信途中で停止")))
        except BaseException as error:
            outcome.append(error)

    operation = threading.Thread(target=synthesize, daemon=True)
    try:
        worker.start()
        old_pid = worker._worker.pid
        operation.start()
        operation.join(timeout=6)
        assert not operation.is_alive(), "部分返信を受信した後も推論期限で終了する"
        assert len(outcome) == 1 and isinstance(outcome[0], ServiceError)
        assert outcome[0].code == "tts_inference_timeout"
        assert not worker.ready and worker._worker is None
        for reply in threading.enumerate():
            if reply.name == "irodori-worker-reply":
                reply.join(timeout=1)
                assert not reply.is_alive()
        worker.start()
        assert worker._worker.pid != old_pid
        assert worker.synthesize(payload()) == wav_bytes()
    finally:
        worker.close()
        if operation.ident is not None:
            operation.join(timeout=2)


def test_voice_validation_bounds_cancelled_io_without_starving_inference(tmp_path, monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    async def scenario() -> None:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        settings = config(tmp_path, max_voice_checks=1)
        register_selected(settings)
        worker = BlockingWorker()
        worker.release.set()
        app = create_app(settings, worker)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = RegisteredVoices.resolve
        def slow_resolve(self, identifier):
            entered.set()
            try:
                if not release.wait(3):
                    raise AssertionError("validation not released")
                return original(self, identifier)
            finally:
                finished.set()
        monkeypatch.setattr(RegisteredVoices, "resolve", slow_resolve)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                async def post():
                    return await client.post("/v1/audio/speech", json=payload().model_dump(),
                                             headers={"X-DS-Environment": "test"})
                pending = asyncio.create_task(post())
                try:
                    async def wait_entered():
                        while not entered.is_set():
                            await asyncio.sleep(0.001)
                    await asyncio.wait_for(wait_entered(), 1)
                    assert (await post()).status_code == 429
                    assert (await client.get("/v1/audio/voices")).status_code == 429
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                    assert (await post()).status_code == 429
                    # 検証I/Oが停止中でも、共有executorを使う推論は進行できる。
                    assert await asyncio.wait_for(app.state.scheduler.submit(payload(), "test"), 1) == wav_bytes()
                finally:
                    release.set()
                    await asyncio.gather(pending, return_exceptions=True)
                assert await asyncio.to_thread(finished.wait, 1)
                await asyncio.sleep(0)
                assert (await post()).status_code == 200
    asyncio.run(scenario())


def test_voice_validation_releases_slot_after_failure() -> None:
    from irodori_service.voice_validation import VoiceValidationPool

    async def scenario() -> None:
        pool = VoiceValidationPool(1)
        def fail():
            raise ServiceError("tts_voice_changed", 409)
        try:
            with pytest.raises(ServiceError, match="tts_voice_changed"):
                await pool.run(fail)
            assert await pool.run(lambda: "recovered") == "recovered"
        finally:
            await pool.close()
    asyncio.run(scenario())
