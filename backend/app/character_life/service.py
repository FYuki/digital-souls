"""現在の許可・正本を再検証する有限の会話外活動。外部I/Oは既存Gateを必ず通す。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

from app.external_mcp import ExecutionContext, ExecutionGate
from app.external_mcp.models import Json, MCPFailure, digest, encode, validate_arguments
from app.inference import InferenceCancellationToken
from app.tool_use.catalog import Candidate, catalog, select_candidates
from app.tool_use.projection import Sanitizer, bounded_json
from app.tool_use.binding import BindingResolver, apply_binding

from .cognition import CognitionPort, PrivacyPort, SCHEMA
from .models import (
    Kind,
    LifeError,
    LifeState,
    ObservationHandoff,
    Result,
    Run,
    StateStatus,
)
from .ports import (
    DeferredMemory,
    DeferredPersonality,
    DeferredReflections,
    MemoryPort,
    PersonalityPort,
    ReflectionSource,
)
from .store import Store
from .formation import LifeFormation

ELYTH_ENDPOINT = "https://elythworld.com/api/mcp/remote"
# 公式の公開情報取得操作に限定する。DM・通知・投稿・既読変更・Fieldは含めない。
ELYTH_TOPIC_TOOLS = frozenset(
    {"get_information", "get_event", "get_aituber", "get_thread", "search_post"}
)


class Service:
    def __init__(
        self,
        store: Store,
        gate: ExecutionGate,
        cognition: CognitionPort,
        privacy: PrivacyPort,
        sanitizer: Sanitizer,
        *,
        foreground_busy: Callable[[], bool],
        timeout: float = 180,
        memory: MemoryPort | None = None,
        personality: PersonalityPort | None = None,
        formation: LifeFormation | None = None,
        reflections: ReflectionSource | None = None,
        bindings: BindingResolver | None = None,
    ) -> None:
        self.store, self.gate, self.cognition = store, gate, cognition
        self.privacy, self.sanitizer = privacy, sanitizer
        self.foreground_busy, self.timeout = foreground_busy, timeout
        # Noneは後続Epicが未接続であることを明示する。接続済みportの一時保留と区別する。
        self.memory, self.personality = memory, personality
        self.formation = formation
        self.reflections = reflections or DeferredReflections()
        self.bindings = bindings or BindingResolver()
        self.cancellations: dict[str, InferenceCancellationToken] = {}
        self.tasks: set[asyncio.Task[str]] = set()
        self.closing = False

    def validate_current(self, run: Run) -> LifeState:
        current = self.store.run(str(run.id))
        if current.attempt != run.attempt:
            raise LifeError(Result.SUPERSEDED, "attempt_superseded")
        if self.closing or current.phase == "paused":
            raise LifeError(Result.DEFERRED, "activity_paused")
        state = self.store.state(run.character_id, run.state_id)
        if state.source == "reflection":
            revisions = self.reflections.current_revisions(run.character_id)
            if revisions is None:
                raise LifeError(Result.DEFERRED, "reflection_source_unavailable")
            self.store.reconcile_reflections(run.character_id, revisions)
            state = self.store.state(run.character_id, run.state_id)
        if (
            state.revision != run.state_revision
            or state.status is not StateStatus.ACTIVE
        ):
            raise LifeError(Result.SUPERSEDED, "intention_changed")
        if (
            state.kind not in {Kind.GOAL_INTENTION, Kind.IMPLEMENTATION_INTENTION}
            or not state.target_id
        ):
            raise LifeError(Result.REJECTED, "goal_intention_required")
        grant = self.store.grant(run.character_id, state.target_id)
        if not grant.enabled or grant.revision != run.grant_revision:
            raise LifeError(Result.REJECTED, "autonomy_grant_changed")
        connection = self.gate.registry.entry(state.target_id).connection
        if connection.identity != grant.connection_identity:
            raise LifeError(Result.REJECTED, "connection_identity_changed")
        if self.foreground_busy():
            raise LifeError(Result.DEFERRED, "foreground_priority")
        if not run.requested and any(r.requested for r in self.store.open_runs()):
            raise LifeError(Result.DEFERRED, "requested_activity_priority")
        return state

    def allowed_candidate(self, candidate: Candidate) -> bool:
        # 未実装の#185を迂回しない。ELYTHは公式操作名をCoreが狭めるだけで、
        # unknown annotationをread/自動retryへ昇格させない。
        config = self.gate.registry.entry(candidate.connection_id).connection.manifest[
            "connection"
        ]
        if config.get("endpoint") == ELYTH_ENDPOINT:
            return candidate.kind == "tool" and candidate.ref in ELYTH_TOPIC_TOOLS
        return True

    def readable_without_recovery(self, candidate: Candidate) -> bool:
        config = self.gate.registry.entry(candidate.connection_id).connection.manifest[
            "connection"
        ]
        return not candidate.may_change_state or (
            config.get("endpoint") == ELYTH_ENDPOINT
            and candidate.ref in ELYTH_TOPIC_TOOLS
        )

    async def execute(self, run_id: str, *, attempt: int | None = None) -> str:
        run = self.store.run(run_id)
        if attempt is not None and attempt != run.attempt:
            return Result.SUPERSEDED
        if run.phase == "finished":
            return str(run.result)
        if run_id in self.cancellations:
            return Result.DEFERRED
        cancellation = InferenceCancellationToken()
        self.cancellations[run_id] = cancellation
        task = asyncio.current_task()
        if task is not None:
            self.tasks.add(task)
        try:
            async with asyncio.timeout(self.timeout):
                return await self._execute(run, cancellation)
        except LifeError as error:
            return str(
                self.store.finish(run, error.result, error.reason).result
                or Result.DEFERRED
            )
        except TimeoutError:
            cancellation.cancel()
            return str(
                self.store.finish(run, Result.DEFERRED, "activity_timeout").result
            )
        except asyncio.CancelledError:
            cancellation.cancel()
            if self.closing:
                return str(
                    self.store.finish(run, Result.DEFERRED, "runtime_stopping").result
                    or Result.DEFERRED
                )
            raise
        except Exception as error:
            # 外部例外は本文もtracebackもDBOSに渡さない。
            logging.getLogger(__name__).warning(
                "Character Life activity failed: exception_type=%s",
                type(error).__name__,
            )
            return str(self.store.finish(run, Result.FAILED, "activity_failed").result)
        finally:
            self.cancellations.pop(run_id, None)
            if task is not None:
                self.tasks.discard(task)

    async def _execute(self, run: Run, cancellation: InferenceCancellationToken) -> str:
        state = self.validate_current(run)
        run = run.model_copy(update={"phase": "running", "reason": "exploring"})
        self.store.save_run(run)
        if run.handoff is not None:
            if not await self.privacy.allowed(run.handoff.topic):
                raise LifeError(Result.REJECTED, "summary_privacy_blocked")
            return await self._finish_handoff(run)
        assert state.target_id is not None
        loop_id = self.gate.begin_loop(
            ExecutionContext(run.character_id, f"life-{run.id}")
        )
        try:
            candidates = select_candidates(
                tuple(
                    c
                    for c in catalog(self.gate, loop_id, self.sanitizer)
                    if c.connection_id == state.target_id and self.allowed_candidate(c)
                ),
                state.content,
                maximum=6,
                schema_budget=10000,
            )
            if not candidates:
                raise LifeError(Result.DEFERRED, "read_capability_unavailable")
            seen: set[str] = set()
            unavailable: set[str] = set()
            results: list[Json] = []
            sources: list[str] = []
            for step in range(8):
                self.validate_current(run)
                decision = await self.cognition.decide(
                    {
                        "goal": state.content,
                        "candidates": [
                            c.projection()
                            for c in candidates
                            if c.id not in unavailable
                        ],
                        "results": results,
                        "finalize_only": step >= 6,
                    },
                    cancellation,
                )
                validate_arguments(SCHEMA, decision)
                self.validate_current(run)
                if decision["action"] == "defer":
                    raise LifeError(Result.DEFERRED, "input_required")
                if decision["action"] == "finish":
                    summary = decision["summary"].strip()
                    if not sources or not summary:
                        return str(
                            self.store.finish(run, Result.NO_CHANGE, "no_topic").result
                        )
                    if not await self.privacy.allowed(summary):
                        if step == 7:
                            raise LifeError(Result.REJECTED, "summary_privacy_blocked")
                        results.append({"outcome": "summary_privacy_blocked"})
                        continue
                    self.validate_current(run)
                    run = run.model_copy(
                        update={
                            "handoff": ObservationHandoff(
                                character_id=run.character_id,
                                topic=summary, source_revisions=tuple(sources)
                            )
                        }
                    )
                    # 外部domainのcommit前に入力を固定し、crash後に別の観測へ作り替えない。
                    self.store.save_run(run)
                    return await self._finish_handoff(run)
                if step >= 6:
                    raise LifeError(Result.DEFERRED, "activity_budget_exhausted")
                candidate = next(
                    (c for c in candidates if c.id == decision["candidate_id"]), None
                )
                if candidate is None:
                    raise LifeError(Result.REJECTED, "invalid_candidate")
                if not self.readable_without_recovery(candidate):
                    # Grantはwriteも対象とするが、#185のConfirmationPolicyPort /
                    # ActionRecoveryPort接続前に副作用を開始しない。
                    raise LifeError(Result.DEFERRED, "action_recovery_unavailable")
                try:
                    arguments = json.loads(decision["arguments_json"])
                    if not isinstance(arguments, dict):
                        raise ValueError
                except ValueError:
                    results.append(
                        {"candidate_id": candidate.id, "outcome": "invalid_arguments"}
                    )
                    continue
                try:
                    binding_id, constraints = self.bindings.resolve(
                        run.character_id,
                        f"life-{run.id}",
                        candidate.connection_id,
                        candidate.ref,
                        explicit=state.binding_target_id or "",
                        required=candidate.binding_required,
                    )
                    arguments = apply_binding(arguments, constraints)
                except MCPFailure as error:
                    raise LifeError(
                        Result.DEFERRED
                        if error.code == "binding_input_required"
                        else Result.REJECTED,
                        "binding_input_required"
                        if error.code == "binding_input_required"
                        else "binding_invalid",
                    ) from None
                try:
                    validate_arguments(json.loads(candidate.schema_json), arguments)
                    if candidate.kind == "resource" and arguments:
                        raise ValueError
                except (ValueError, MCPFailure):
                    # schema不適合の案を外部へ送らず、有限budget内で選び直す。
                    results.append(
                        {"candidate_id": candidate.id, "outcome": "invalid_arguments"}
                    )
                    continue
                fingerprint = digest([candidate.id, arguments])
                if fingerprint in seen:
                    results.append(
                        {"candidate_id": candidate.id, "outcome": "duplicate_read"}
                    )
                    continue
                seen.add(fingerprint)
                if not self.sanitizer.arguments_allowed(arguments) or (
                    bool(arguments)
                    and not await self.privacy.allowed(
                        "外部へ送信する情報: " + encode(arguments)
                    )
                ):
                    raise LifeError(Result.REJECTED, "egress_privacy_blocked")
                approved_at = time.monotonic()

                def guard() -> None:
                    self.validate_current(run)
                    if time.monotonic() - approved_at > 30 or cancellation.is_cancelled:
                        raise LifeError(Result.DEFERRED, "egress_approval_expired")

                if candidate.kind == "tool":
                    envelope = await self.gate.invoke(
                        candidate.connection_id,
                        candidate.ref,
                        arguments,
                        loop_id,
                        binding_id=binding_id,
                        dispatch_guard=guard,
                    )
                else:
                    envelope = await self.gate.read_resource(
                        candidate.connection_id,
                        candidate.ref,
                        loop_id,
                        binding_id=binding_id,
                        dispatch_guard=guard,
                    )
                self.store.record_operation(
                    run,
                    execution_id=envelope["execution_id"],
                    candidate_id=candidate.id,
                    operation_label=self.sanitizer.text(candidate.name, maximum=160),
                    snapshot_revision=candidate.snapshot_revision,
                    argument_fingerprint=fingerprint,
                    outcome=envelope["outcome"],
                )
                self.validate_current(run)
                if envelope.get("error_category") == "tool_error":
                    # 読取先に現在の対象がない場合などは、別の候補を選び直せる。
                    # 外部エラー本文は推論へ入れず、成功した観測とも数えない。
                    results.append(
                        {"candidate_id": candidate.id, "outcome": "tool_error"}
                    )
                    unavailable.add(candidate.id)
                    continue
                if envelope["outcome"] != "succeeded":
                    raise LifeError(
                        Result.DEFERRED
                        if envelope["outcome"]
                        in {"input_required", "unavailable", "budget_exceeded"}
                        else Result.FAILED,
                        "external_operation_incomplete",
                    )
                projected = self.sanitizer.result(envelope, may_change_state=False)
                results.append(json.loads(bounded_json(projected, 3000)))
                sources.append(
                    candidate.snapshot_revision + ":" + envelope["execution_id"]
                )
            raise LifeError(Result.DEFERRED, "activity_budget_exhausted")
        finally:
            self.gate.end_loop(loop_id)
            self.bindings.forget(run.character_id, f"life-{run.id}")

    def pause(self, run_id: str) -> None:
        run = self.store.run(run_id)
        if run.phase == "finished":
            return
        self.store.save_run(
            run.model_copy(update={"phase": "paused", "reason": "user_paused"})
        )
        cancellation = self.cancellations.get(run_id)
        if cancellation:
            cancellation.cancel()

    async def _finish_handoff(self, run: Run) -> str:
        # model_copyを含む内部更新でも、別characterの作業記録を接続先へ渡さない。
        run = Run.model_validate(run.model_dump())
        self.validate_current(run)
        handoff = run.handoff
        assert handoff is not None
        # #100/#101はrun_idで冪等に受理する契約。本文・時刻・sourceは再開前後で同じ。
        try:
            memory_result = await (self.memory or DeferredMemory()).record_observation(
                character=run.character_id,
                run_id=str(run.id),
                experienced_at=handoff.experienced_at,
                topic=handoff.topic,
                source_revisions=handoff.source_revisions,
            )
        except Exception:
            memory_result = Result.FAILED
        personality_result = Result.DEFERRED
        if memory_result in {Result.APPLIED, Result.NO_CHANGE}:
            self.validate_current(run)
            try:
                personality_result = await (self.personality or DeferredPersonality()).evaluate(
                    run.character_id, str(run.id)
                )
            except Exception:
                personality_result = Result.FAILED
        self.validate_current(run)
        incomplete = {Result.FAILED, Result.RESULT_UNKNOWN, Result.DEFERRED}
        if (
            self.memory is not None and memory_result in incomplete
        ) or (
            self.personality is not None and personality_result in incomplete
        ):
            # 確定済みhandoffを残し、同じidempotency keyで通常のresumeから再試行する。
            return str(self.store.finish(
                run, Result.DEFERRED, "dependency_handoff_failed",
                sources=handoff.source_revisions,
                dependencies={"episode": memory_result, "personality": personality_result},
            ).result)
        return str(
            self.store.finish(
                run,
                Result.APPLIED,
                "topic_ready",
                summary=handoff.topic,
                sources=handoff.source_revisions,
                dependencies={
                    "episode": memory_result,
                    "personality": personality_result,
                },
            ).result
        )

    async def form_life_states(self, character: str) -> str:
        if self.closing or self.foreground_busy() or self.formation is None:
            return Result.DEFERRED

        cancellation = InferenceCancellationToken()

        def check_current() -> None:
            # 形成開始後に会話・利用者要求が発生した場合も、次の推論・保存へ進まない。
            if self.closing or self.foreground_busy():
                cancellation.cancel()
                raise LifeError(Result.DEFERRED, "foreground_priority")
            if any(run.requested for run in self.store.open_runs()):
                cancellation.cancel()
                raise LifeError(Result.DEFERRED, "requested_activity_priority")

        task = asyncio.current_task()
        interruption = Result.DEFERRED
        monitor_cancellation = object()
        initial_cancellations = task.cancelling() if task is not None else 0
        body_completed = False

        async def monitor_priority() -> None:
            nonlocal interruption
            while True:
                await asyncio.sleep(0.05)
                if body_completed:
                    return
                try:
                    check_current()
                except Exception as exc:
                    interruption = exc.result if isinstance(exc, LifeError) else Result.FAILED
                    if not isinstance(exc, LifeError):
                        logging.getLogger(__name__).warning(
                            "Character Life formation failed: exception_type=%s",
                            type(exc).__name__,
                        )
                    cancellation.cancel()
                    if task is not None:
                        task.cancel(monitor_cancellation)
                    return

        monitor = asyncio.create_task(monitor_priority())
        if task is not None:
            self.tasks.add(task)
        try:
            try:
                async with asyncio.timeout(self.timeout):
                    check_current()
                    await (self.memory or DeferredMemory()).catch_up(character)
                    return await self.formation.run(
                        character, check_current=check_current, cancellation=cancellation
                    )
            finally:
                # cleanupのawaitへ入る前に監視による新しい中断を止める。
                body_completed = True
                cancellation.cancel()
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
        except LifeError as error:
            return error.result
        except TimeoutError:
            return Result.DEFERRED
        except asyncio.CancelledError as error:
            if error.args == (monitor_cancellation,):
                if task is not None and task.uncancel() > initial_cancellations:
                    # 同時に届いた外部cancelまで、監視の取消しとして消費しない。
                    raise
                return interruption
            if self.closing:
                return Result.DEFERRED
            raise
        except Exception as error:
            # 外部例外の本文・tracebackは記録せず、診断には型名だけを残す。
            logging.getLogger(__name__).warning(
                "Character Life formation failed: exception_type=%s",
                type(error).__name__,
            )
            return Result.FAILED
        finally:
            if task is not None:
                self.tasks.discard(task)

    async def stop(self) -> None:
        self.closing = True
        for token in tuple(self.cancellations.values()):
            token.cancel()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
