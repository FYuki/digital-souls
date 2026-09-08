"""同じ実推論serverで背景認知と会話が重なる条件を測る。本文は記録しない。"""

import asyncio
import shutil
import subprocess
import time
from uuid import uuid4

from app.character_life.models import Result
from app.inference import InferenceCaller, InferenceMessage, InferenceTarget


async def measure_priority(runtime, state, router, prompt, count_calls):
    entered = asyncio.Event()
    service = runtime.service
    original_decide = service.cognition.decide
    original_busy = service.foreground_busy
    foreground = False

    async def tracked(context, cancellation):
        entered.set()
        return await original_decide(context, cancellation)

    service.cognition.decide = tracked
    service.foreground_busy = lambda: foreground
    gpu = shutil.which("nvidia-smi")
    samples = []
    stop_monitor = asyncio.Event()

    async def monitor():
        while not stop_monitor.is_set():
            if gpu:
                try:
                    sample = await asyncio.to_thread(
                        subprocess.run,
                        [
                            gpu,
                            "--query-gpu=memory.used,utilization.gpu",
                            "--format=csv,noheader,nounits",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    if sample.returncode == 0:
                        try:
                            samples.extend(
                                tuple(int(v.strip()) for v in row.split(","))
                                for row in sample.stdout.splitlines()
                            )
                        except ValueError:
                            pass
                except subprocess.TimeoutExpired:
                    pass
            try:
                await asyncio.wait_for(stop_monitor.wait(), 0.5)
            except TimeoutError:
                pass

    monitor_task = asyncio.create_task(monitor())
    try:
        pending = await runtime.submit("miori", state.id, str(uuid4()), requested=False)
        await asyncio.wait_for(entered.wait(), 15)
        foreground = True
        before_calls = count_calls()
        started = time.monotonic()
        first_delta = None
        async for delta in router.stream_text(
            caller=InferenceCaller.CHAT,
            target=InferenceTarget.CHAT,
            messages=tuple(
                InferenceMessage(m.role.value, m.content) for m in prompt.messages
            ),
        ):
            if delta and first_delta is None:
                first_delta = time.monotonic() - started
        duration = time.monotonic() - started
        assert first_delta is not None
        for _ in range(200):
            deferred = service.store.run(str(pending.id))
            if deferred.phase == "finished":
                break
            await asyncio.sleep(0.1)
        assert deferred.result is Result.DEFERRED
        assert deferred.reason == "foreground_priority"
        assert count_calls() == before_calls
        return {
            "samples": 1,
            "overlap_chat_ttft_seconds": round(first_delta, 3),
            "overlap_chat_duration_seconds": round(duration, 3),
            "gpu_memory_peak_mib": max(s[0] for s in samples) if samples else None,
            "gpu_utilization_peak_percent": max(s[1] for s in samples)
            if samples
            else None,
            "background_result": deferred.result,
            "dispatch_during_foreground": 0,
        }
    finally:
        stop_monitor.set()
        try:
            await monitor_task
        finally:
            service.cognition.decide = original_decide
            service.foreground_busy = original_busy
