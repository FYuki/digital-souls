"""永続購読、独立consumer、有限pollとMCP wake-upのライフサイクル。"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.external_mcp import ExecutionContext, ExecutionGate
from app.external_mcp.models import Json, MCPFailure, digest, encode
from app.tool_use.projection import Sanitizer
from .contracts import Baseline, Event, Source, failure, token
from .reader import MCPEventReader
from .store import EventStore, context_json


@dataclass(frozen=True)
class Delivery:
    events: tuple[Json, ...]
    gaps: tuple[Json, ...]
    snapshot: Json | None
    receipt: str
    recovery: Json | None = None


class EventRuntime:
    def __init__(
        self, gate: ExecutionGate, sources: tuple[Source, ...], path: Path,
        sanitizer: Sanitizer, *, clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        # 同一DBを複数backendが取得しない。process終了時はOSがleaseを解放する。
        import fcntl
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lease_path = path.with_suffix(".lock")
        if lease_path.is_symlink():
            raise failure("unsafe_event_store")
        self._lease = lease_path.open("a")
        try:
            fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lease.close()
            raise failure("event_store_owned") from None
        self.sources = {source.id: source for source in sources}
        if len(self.sources) != len(sources) or len(sources) > 32 or len({s.connection_id for s in sources}) != len(sources):
            self._lease.close()
            raise failure("invalid_event_sources")
        self.store = EventStore(path, clock=clock)
        try:
            for source in sources:
                self.store.configure(source)
                self.store.prune(source)
            self.store.suspend_unconfigured(set(self.sources))
        except BaseException:
            self.store.close()
            self._lease.close()
            raise
        self.reader = MCPEventReader(gate, self.store, sanitizer)
        self.monotonic = monotonic
        self._locks = {s.id: asyncio.Lock() for s in sources}
        self._signals = {s.id: asyncio.Event() for s in sources}
        self._workers: list[asyncio.Task[None]] = []
        self._watches: dict[str, asyncio.Task[None]] = {}
        self._watch_generation: dict[str, int] = {}
        self._watch_retry: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._not_before: dict[str, float] = {}
        self._closed = False

    def _source(self, source_id: str) -> Source:
        if self._closed:
            raise failure("event_runtime_closed")
        try:
            return self.sources[source_id]
        except KeyError:
            raise failure("unknown_event_source") from None

    def _consumer(self, source: Source, consumer: str, context: ExecutionContext) -> Json:
        current = self.store.consumer(source.id, consumer)
        if current["context"] != context_json(context):
            raise failure("event_consumer_identity_changed")
        return current

    @staticmethod
    def _check_baseline(baseline: Baseline, current: Json) -> None:
        if baseline.epoch == current["epoch"] and baseline.position < current["position"]:
            raise failure("event_snapshot_rewound")

    async def subscribe(
        self, source_id: str, consumer_id: str, context: ExecutionContext,
    ) -> str:
        source = self._source(source_id)
        token(consumer_id, self.reader.sanitizer)
        if context.session_id != source.context.session_id:
            raise failure("invalid_event_budget_context")
        for item in (context.character_id, context.user_id, context.binding_id):
            if item is not None:
                token(item, self.reader.sanitizer)
        async with self._locks[source.id]:
            previous = {c["id"] for c in self.store.consumers(source.id)}
            self.store.register(source.id, consumer_id, context)
            try:
                await self.reader.authorize(source, context)
                if consumer_id not in previous:
                    baseline = await self.reader.snapshot(source, context)
                    self._check_baseline(baseline, self.store.state(source.id))
                    if not previous or self.store.state(source.id)["epoch"] is None:
                        self.store.baseline(source.id, baseline, gap=False)
                    self.store.initial(source.id, consumer_id, baseline)
            except BaseException:
                if consumer_id not in previous:
                    self.store.unregister(source.id, consumer_id)
                raise
        self._signals[source.id].set()
        return consumer_id

    async def unsubscribe(self, source_id: str, consumer_id: str, context: ExecutionContext) -> None:
        source = self._source(source_id)
        async with self._locks[source.id]:
            self._consumer(source, consumer_id, context)
            # 権限を取り消した後も明示解除できる。利用者同一性は保持する。
            self.store.unregister(source.id, consumer_id)
            if not self.store.consumers(source.id):
                await self._cancel_watch(source.id)
        self._signals[source.id].set()

    async def poll_once(self, source_id: str) -> bool:
        source = self._source(source_id)
        async with self._locks[source.id]:
            self.store.prune(source)
            if not self.store.consumers(source.id):
                self.store.set_status(source.id, "idle")
                return False
            if self.store.state(source.id)["status"] == "stopped":
                return False
            if self.monotonic() < self._not_before.get(source.id, 0):
                return False
            state = self.store.state(source.id)
            try:
                await self.reader.authorize(source, source.context)
                if state["epoch"] is None:
                    self.store.baseline(source.id, await self.reader.snapshot(source, source.context), gap=False)
                    more = False
                else:
                    page = await self.reader.history(
                        source, source.context, cursor=state["cursor"], after=state["position"], epoch=state["epoch"],
                    )
                    if page.gap:
                        try:
                            baseline = await self.reader.snapshot(source, source.context)
                            self._check_baseline(baseline, state)
                        except MCPFailure:
                            # 履歴欠落後のsnapshot未確定は、元cursorを保って再確認する。
                            raise failure("event_snapshot_pending") from None
                        self.store.baseline(source.id, baseline, gap=True)
                        more = False
                    else:
                        self.store.commit_page(source, page, expected_cursor=state["cursor"])
                        more = page.more
                self._failures[source.id] = 0
                # 通知storm・空のmoreを抑え、共有budgetも併用する。
                self._not_before[source.id] = self.monotonic() + min(1.0, source.limits.poll_seconds)
                return more
            except MCPFailure as error:
                unsafe = error.category == "event" and error.code not in {
                    "event_result_unavailable", "event_source_unsubscribed", "event_snapshot_pending",
                }
                self.store.set_status(source.id, "stopped" if unsafe else "backoff", self._reason(error))
                count = min(16, self._failures.get(source.id, 0) + 1)
                self._failures[source.id] = count
                self._not_before[source.id] = self.monotonic() + min(
                    source.limits.retry_max, source.limits.retry_initial * 2 ** (count - 1),
                )
                return False

    @staticmethod
    def _reason(error: MCPFailure) -> str:
        # 外部例外本文・URI・cursorを診断に転記しない。
        if error.code in {"rate_limit_exceeded", "budget_exceeded"}:
            return "budget"
        if error.category == "event":
            return "event_contract_or_registration" if error.code != "event_result_unavailable" else "result_unavailable"
        return {"policy": "authorization", "budget": "budget", "transport": "transport",
                "authentication": "authentication", "protocol": "protocol"}.get(error.category, "unavailable")

    async def resume(self, source_id: str) -> None:
        source = self._source(source_id)
        async with self._locks[source.id]:
            await self.reader.authorize(source, source.context)
            # 停止原因を修正した管理側だけが再試行する。cursorを飛ばさない。
            self.store.set_status(source.id, "ready")
            self._not_before[source.id] = 0
        self._signals[source.id].set()

    async def read(self, source_id: str, consumer_id: str, context: ExecutionContext) -> Delivery:
        source = self._source(source_id)
        async with self._locks[source.id]:
            current = self._consumer(source, consumer_id, context)
            def guard() -> None:
                self._consumer(source, consumer_id, context)
            await self.reader.authorize(source, context, guard=guard, delivery=True)
            self.store.prune(source)
            state = self.store.state(source.id)
            if current["epoch"] is None:
                raise failure("event_baseline_pending")
            snapshot = json.loads(current["initial_state"]) if current["initial"] else None
            gaps: list[Json] = []
            recovery: Json | None = None
            events: tuple[Event, ...] = ()
            epoch, cursor, pos, stream = (current[k] for k in ("epoch", "cursor", "position", "stream"))
            if epoch == state["epoch"] and stream == state["stream"] and pos >= state["floor"]:
                # 同期区間の最初の期限判定と同じfloorで読む。再判定で欠落範囲を飛ばさない。
                events = self.store.buffered(source, pos, prune=False)
                if events:
                    cursor, pos = events[-1].cursor, events[-1].position
            else:
                # 遅いconsumerだけがproducer履歴を再読する。共通bufferの保持時刻は延長しない。
                recovery = {"reason": "buffer_unavailable", "after": pos, "replayed": True}
                page = await self.reader.history(source, context, cursor=cursor, after=pos, epoch=epoch)
                if page.gap:
                    baseline = await self.reader.snapshot(source, context)
                    self._check_baseline(baseline, current)
                    gaps.append({"reason": "history_unavailable", "after": current["position"],
                                 "resume_position": baseline.position, "epoch_changed": epoch != baseline.epoch})
                    snapshot = json.loads(baseline.metadata_json)
                    epoch, cursor, pos, stream = baseline.epoch, baseline.cursor, baseline.position, state["stream"]
                else:
                    events, cursor, pos = page.events, page.cursor, page.position
                    if epoch == state["epoch"]:
                        stream = state["stream"]
            output: list[Json] = []
            for event in events:
                if event.reason:
                    gaps.append({"reason": "invalid_event", "position": event.position})
                else:
                    # 永続化後に秘密値登録が増えた場合も再検査する。
                    metadata = event.metadata
                    if self.reader.sanitizer.text(encode(metadata)) != encode(metadata):
                        gaps.append({"reason": "privacy_changed", "position": event.position})
                        continue
                    output.append({"key": event.key, "position": event.position, "metadata": metadata})
            # replay中にgrant・definition・bindingが変わっても配信しない。
            await self.reader.authorize(source, context, guard=guard)
            receipt = self.store.offer(source.id, consumer_id, epoch=epoch, cursor=cursor,
                                       position=pos, stream=stream, gaps=gaps, initial=bool(current["initial"]))
            if snapshot is not None and self.reader.sanitizer.text(encode(snapshot)) != encode(snapshot):
                snapshot = {}
            return Delivery(tuple(output), tuple(gaps), snapshot, receipt, recovery)

    async def acknowledge(
        self, source_id: str, consumer_id: str, receipt: str, context: ExecutionContext,
        *, accept_gap: bool = False,
    ) -> None:
        source = self._source(source_id)
        async with self._locks[source.id]:
            def guard() -> None:
                self._consumer(source, consumer_id, context)
            guard()
            await self.reader.authorize(source, context, guard=guard)
            self.store.acknowledge(source.id, consumer_id, receipt, accept_gap=accept_gap)

    def diagnostics(self) -> tuple[Json, ...]:
        return tuple(self.store.diagnostics(source) for source in self.sources)

    async def _cancel_watch(self, source_id: str) -> None:
        task = self._watches.pop(source_id, None)
        self._watch_generation.pop(source_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _watch(self, source: Source) -> None:
        assert source.wake_resource is not None
        try:
            await self.reader.authorize(source, source.context)
            # wake Resourceにも同じ対象bindingを適用する。
            from .contracts import Operation
            context = self.reader.context(
                source, Operation("resource", source.wake_resource, ""), source.context,
            )
            await self.reader.gate.event_notifications(
                source.connection_id, source.wake_resource, context,
                guard=lambda: self.reader.guard(source), wake=self._signals[source.id].set,
            )
        except Exception:
            # optional wakeの内部障害も本文を記録せず、定期取得を継続する。
            pass
        finally:
            self._watch_retry[source.id] = self.monotonic() + source.limits.retry_max

    async def _manage_watch(self, source: Source) -> None:
        if source.wake_resource is None:
            return
        try:
            entry = self.reader.gate.registry.entry(source.connection_id)
            permitted = bool(self.store.consumers(source.id)) and entry.desired_enabled and entry.availability in {"available", "degraded"}
            generation = entry.generation
        except MCPFailure:
            permitted, generation = False, -1
        task = self._watches.get(source.id)
        if task is not None and (task.done() or not permitted or self._watch_generation.get(source.id) != generation):
            await self._cancel_watch(source.id)
        if permitted and source.id not in self._watches and self.monotonic() >= self._watch_retry.get(source.id, 0):
            self._watches[source.id] = asyncio.create_task(self._watch(source))
            self._watch_generation[source.id] = generation

    async def _worker(self, source: Source) -> None:
        signal = self._signals[source.id]
        due = self.monotonic()
        while True:
            signal.clear()
            more = False
            if self.monotonic() >= due:
                try:
                    more = await self.poll_once(source.id)
                except Exception:
                    self.store.set_status(source.id, "stopped", "internal_error")
                due = max(self._not_before.get(source.id, 0), self.monotonic() + (1.0 if more else source.limits.poll_seconds))
            await self._manage_watch(source)
            try:
                # 最大1秒で解除・世代変更を確認する。履歴を1秒pollするわけではない。
                await asyncio.wait_for(signal.wait(), timeout=max(.01, min(1.0, due - self.monotonic())))
                due = max(self.monotonic(), self._not_before.get(source.id, 0))
            except TimeoutError:
                pass

    def start(self) -> None:
        if self._closed:
            raise failure("event_runtime_closed")
        if not self._workers:
            self._workers = [asyncio.create_task(self._worker(s), name=f"event-{digest(s.id)[7:19]}") for s in self.sources.values()]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        for source_id in list(self._watches):
            await self._cancel_watch(source_id)
        self.store.close()
        self._lease.close()
