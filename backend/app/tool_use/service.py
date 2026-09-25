"""テキスト・音声が共有する、有限のTool loopと入力待ち。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from uuid import uuid4
from dataclasses import dataclass, field
from typing import Callable, cast
from contextlib import contextmanager
from collections.abc import Iterator, Awaitable

from app.addon_action.interaction import confirmation_resume_id
from app.external_mcp import ExecutionContext, ExecutionGate
from app.external_mcp.models import Json, MCPFailure, encode
from app.inference import InferenceCancellationToken, InferenceError

from .binding import BindingResolver
from .catalog import Candidate
from . import decision_loop as _decision_loop
from .decision_loop import DecisionLoop
from .projection import Sanitizer
from .routing import DecisionPort

logger = logging.getLogger(__name__)
TOOL_STOP_MESSAGE = "tool_operation_stopped"

CONFIRMATION_REQUIRED_MESSAGE = _decision_loop.CONFIRMATION_REQUIRED_MESSAGE
CONFIRMATION_WAITING_MESSAGE = _decision_loop.CONFIRMATION_WAITING_MESSAGE


@dataclass(frozen=True)
class ToolMaterial:
    results: tuple[Json, ...] = field(default=(), repr=False)
    direct_text: str | None = None
    sources: tuple[Json, ...] = ()
    waiting: bool = False


@dataclass
class _Run:
    loop: str
    context: ExecutionContext
    original_request: str = field(repr=False)
    cycle: int = 1
    user_followup: bool = False
    clarification: list[Json] = field(default_factory=list, repr=False)
    results: list[Json] = field(default_factory=list, repr=False)
    sources: list[Json] = field(default_factory=list)
    forbidden: set[str] = field(default_factory=set)
    completed_calls: set[str] = field(default_factory=set)
    confirmation: str | None = None
    continuation_claimed: bool = False
    confirmation_guard: Callable[[], Awaitable[None]] | None = field(
        default=None, repr=False
    )
    interaction: str | None = None
    answer_schema: Json | None = field(default=None, repr=False)
    requests: Json = field(default_factory=dict, repr=False)
    candidate: Candidate | None = None
    binding_id: str | None = None
    call_fingerprint: str | None = None
    binding_candidate: Candidate | None = None
    binding_arguments: Json = field(default_factory=dict, repr=False)
    waiting_until: float | None = None
    expiration: asyncio.TimerHandle | None = None
    presence_expiration: asyncio.TimerHandle | None = None
    task: asyncio.Task[object] | None = None
    cancellation: InferenceCancellationToken = field(
        default_factory=InferenceCancellationToken
    )


class ToolService:
    def __init__(
        self,
        gate: ExecutionGate,
        router: DecisionPort,
        sanitizer: Sanitizer,
        bindings: BindingResolver,
        *,
        clock: Callable[[], float] = time.monotonic,
        input_timeout: float = 600,
        max_sessions: int = 128,
        protected_roots: tuple[Path, ...] = (),
        turn_timeout: float = 120,
    ) -> None:
        if input_timeout <= 0 or max_sessions < 1 or turn_timeout <= 0:
            raise ValueError("invalid tool service limits")
        self.gate, self.router = gate, router
        self.sanitizer, self.bindings = sanitizer, bindings
        self.clock, self.input_timeout = clock, input_timeout
        self.max_sessions = max_sessions
        self.turn_timeout = turn_timeout
        self.protected_roots = tuple(p.resolve() for p in protected_roots)
        self._runs: dict[tuple[str, str], _Run] = {}
        self._status: dict[tuple[str, str], Json] = {}
        self._owners: dict[tuple[str, str], asyncio.Task[object]] = {}
        self.closing = False
        self._decision_loop = DecisionLoop(self)

    @contextmanager
    def response_scope(self, character: str, conversation: str) -> Iterator[None]:
        """最終回答・履歴保存まで同じ停止要求に従う。"""
        key = (character, conversation)
        task = asyncio.current_task()
        assert task is not None
        previous = self._owners.get(key)
        if previous is not None and previous is not task:
            self.stop(*key)
        self._owners[key] = task
        try:
            yield
        finally:
            if self._owners.get(key) is task:
                self._owners.pop(key)
                if key not in self._runs and key in self._status:
                    self._status[key]["state"] = "idle"

    def status(self, character: str, conversation: str) -> Json:
        return cast(
            Json,
            json.loads(
                encode(
                    self._status.get(
                        (character, conversation), {"state": "idle", "sources": []}
                    )
                )
            ),
        )

    def pending_confirmation(
        self, character: str, conversation: str, request_id: str
    ) -> bool:
        run = self._runs.get((character, conversation))
        return bool(
            run
            and run.confirmation == request_id
            and run.waiting_until is not None
            and self.clock() < run.waiting_until
            and run.task is None
        )

    def claim_confirmation(
        self, character: str, conversation: str, request_id: str
    ) -> bool:
        if not self.pending_confirmation(character, conversation, request_id):
            return False
        run = self._runs[(character, conversation)]
        if run.continuation_claimed:
            return False
        run.continuation_claimed = True
        return True

    def release_confirmation(
        self, character: str, conversation: str, request_id: str
    ) -> None:
        run = self._runs.get((character, conversation))
        if run is not None and run.confirmation == request_id:
            run.continuation_claimed = False

    def heartbeat(self, character: str, conversation: str) -> None:
        # HTTPの入力待ちは常時接続を持たないため、画面の消失も有限時間で検出する。
        key = (character, conversation)
        run = self._runs.get(key)
        if run is None:
            return
        if run.presence_expiration:
            run.presence_expiration.cancel()

        def expire() -> None:
            if self._runs.get(key) is run:
                self.stop(*key)

        run.presence_expiration = asyncio.get_running_loop().call_later(45, expire)

    def interrupted(self, character: str, conversation: str, reason: str) -> None:
        run = self._runs.get((character, conversation))
        # 質問の読み上げ中に利用者が回答を始めても、入力待ちを破棄しない。
        if reason == "barge_in" and run is not None and run.waiting_until is not None:
            return
        self.stop(character, conversation)

    def stop(self, character: str, conversation: str) -> None:
        key = (character, conversation)
        owner = self._owners.pop(key, None)
        run = self._runs.pop(key, None)
        targets = {
            task for task in (owner, run.task if run else None) if task is not None
        }
        for task in targets:
            if task is not asyncio.current_task():
                task.cancel(TOOL_STOP_MESSAGE)
        self._clean_run(key, run)

    def _clean_run(self, key: tuple[str, str], run: _Run | None) -> None:
        if run is not None:
            run.cancellation.cancel()
            self.gate.end_loop(run.loop)
            if run.expiration:
                run.expiration.cancel()
            if run.presence_expiration:
                run.presence_expiration.cancel()
        self.bindings.forget(*key)
        self._status.pop(key, None)

    def end_idle_confirmations(self, request_ids: tuple[str, ...]) -> None:
        """正本で終了した承認待ちだけを破棄し、開始済み処理や返答のownerは中断しない。"""
        ended = set(request_ids)
        for key, run in tuple(self._runs.items()):
            if run.confirmation in ended and run.task is None:
                self._runs.pop(key)
                self._clean_run(key, run)

    def connection_disabled(self, connection_id: str) -> None:
        # 回答・binding待ちだけを終了する。既に送信した外部処理はGateの世代で再送を防ぐ。
        for key, run in tuple(self._runs.items()):
            candidate = run.binding_candidate or (
                run.candidate if run.interaction or run.confirmation else None
            )
            if (
                run.waiting_until is not None
                and candidate is not None
                and candidate.connection_id == connection_id
            ):
                self.stop(*key)

    def close(self) -> None:
        self.closing = True
        for character, conversation in tuple(
            self._runs.keys() | self._owners.keys() | self._status.keys()
        ):
            self.stop(character, conversation)

    async def run(
        self,
        character: str,
        conversation: str,
        request: str,
        *,
        history: tuple[Json, ...] = (),
        before_execute: Callable[[], Awaitable[None]] | None = None,
    ) -> ToolMaterial:
        if self.closing:
            raise asyncio.CancelledError()
        key = (character, conversation)
        run = self._runs.get(key)
        if run and run.task and not run.task.done():
            self.stop(*key)
            run = None
        if run and run.waiting_until is not None and self.clock() >= run.waiting_until:
            self.stop(*key)
            return ToolMaterial(
                direct_text="追加情報の回答期限が切れました。必要なら改めて依頼してください。"
            )
        explicit_resume = confirmation_resume_id(character, conversation)
        if explicit_resume is not None and (
            run is None or run.confirmation != explicit_resume
        ):
            return ToolMaterial(
                direct_text="この操作の待機は終了しています。承認は今後の利用に適用します。"
            )
        if (
            run is not None
            and run.confirmation is not None
            and explicit_resume != run.confirmation
        ):
            # STTやLLM判断で承認を推測しない。待機期限も延長しない。
            return self._material(
                run,
                CONFIRMATION_WAITING_MESSAGE,
                waiting=True,
            )
        if run is not None:
            run.continuation_claimed = False
            run.user_followup = True
            if run.clarification and run.clarification[-1]["answer"] is None:
                # 通常履歴の切り詰めに依存せず、直前の確認に対する回答を保持する。
                run.clarification[-1]["answer"] = request
        if run is None:
            if len(self._runs) >= self.max_sessions:
                return ToolMaterial(
                    direct_text="外部ツールの受付が混み合っています。少し待ってから再度依頼してください。"
                )
            context = ExecutionContext(
                character, conversation, action_scope=str(uuid4())
            )
            run = _Run(self.gate.begin_loop(context), context, request)
            self._runs[key] = run
        if run.expiration:
            run.expiration.cancel()
        run.task = asyncio.current_task()
        run.waiting_until = None
        if key not in self._status and len(self._status) >= self.max_sessions:
            expired = next(
                (
                    saved
                    for saved in self._status
                    if saved not in self._runs and saved not in self._owners
                ),
                None,
            )
            if expired is not None:
                self.stop(*expired)
        self._status[key] = {"state": "running", "sources": []}
        keep = False
        try:
            async with asyncio.timeout(self.turn_timeout):
                material = await self._drive(run, request, history, before_execute)
            keep = material.waiting
            self._status[key] = {
                "state": "waiting" if keep else "idle",
                "sources": [
                    {"label": s["label"], "source_id": s["source_id"]}
                    for s in material.sources
                ],
                **({"confirmation_id": run.confirmation} if run.confirmation else {}),
            }
            if keep:
                run.waiting_until = self.clock() + self.input_timeout
                run.expiration = asyncio.get_running_loop().call_later(
                    self.input_timeout, self._expire, key, run
                )
            return material
        except (MCPFailure, InferenceError) as error:
            logger.warning(
                "Tool routing stopped: category=%s code=%s",
                error.category,
                error.code if isinstance(error, MCPFailure) else "inference_failed",
            )
            return self._material(
                run,
                "外部ツールを利用できませんでした。操作の成功は確認できていません。",
            )
        except TimeoutError:
            run.cancellation.cancel()
            return self._material(
                run,
                "外部ツールの待ち時間が上限に達しました。開始済み操作の結果は確認できていません。",
            )
        except asyncio.CancelledError:
            run.cancellation.cancel()
            raise
        finally:
            if not keep and self._runs.get(key) is run:
                if run.presence_expiration:
                    run.presence_expiration.cancel()
                self.gate.end_loop(run.loop)
                self._runs.pop(key)
                self._status[key] = {
                    "state": "running"
                    if key in self._owners and run.results
                    else "idle",
                    "sources": [
                        {"label": s["label"], "source_id": s["source_id"]}
                        for s in run.sources
                    ],
                }
            run.task = None

    def _expire(self, key: tuple[str, str], run: _Run) -> None:
        if self._runs.get(key) is run and run.task is None:
            self.stop(*key)

    def _material(
        self, run: _Run, text: str | None = None, *, waiting: bool = False
    ) -> ToolMaterial:
        if text is not None and run.results and not waiting:
            return ToolMaterial(
                (*run.results, {"outcome": "incomplete", "reason": text}),
                None,
                tuple(run.sources),
                False,
            )
        return ToolMaterial(tuple(run.results), text, tuple(run.sources), waiting)

    async def _drive(
        self,
        run: _Run,
        request: str,
        history: tuple[Json, ...],
        before_execute: Callable[[], Awaitable[None]] | None,
    ) -> ToolMaterial:
        return await self._decision_loop.drive(
            run, request, history, before_execute
        )

    def _protect_core(self, arguments: Json) -> None:
        """既存の内部検査呼出しを、移設先へ委譲する。"""
        self._decision_loop._protect_core(arguments)

    def _interaction(self, envelope: Json) -> tuple[Json, Json]:
        """既存の内部入力要求検証を、移設先へ委譲する。"""
        return self._decision_loop._interaction(envelope)

    @staticmethod
    def _validate_interaction_schema(schema: Json) -> None:
        """既存の内部schema検証を、移設先へ委譲する。"""
        DecisionLoop._validate_interaction_schema(schema)
