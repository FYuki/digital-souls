from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Callable

from app.addon_action.interaction import confirmation_resume_id
from app.external_mcp.models import Json, MCPFailure, digest, encode, input_validator, validate_arguments
from app.privacy.contracts import ScanSuccess

from .binding import apply_binding
from .catalog import Candidate, catalog, select_candidates
from .projection import bounded_json
from .routing import parse_object

if TYPE_CHECKING:
    from .service import ToolMaterial, ToolService, _Run

_PROTECTED_INPUT = re.compile(
    r"password|secret|credential|permission|authorization|api.?key|token|"
    r"パスワード|秘密|認証|許可|権限|設定変更|コード変更|DB変更",
    re.I,
)
_REFRESH = re.compile(r"refresh|更新|再取得", re.I)
logger = logging.getLogger(__name__)
CONFIRMATION_REQUIRED_MESSAGE = (
    "操作の承認が必要です。画面で「常に承認する」「一度承認する」「拒否する」から選んでください。"
    "一度の承認はツール呼び出し1回分です。"
)
CONFIRMATION_WAITING_MESSAGE = "操作の承認をお待ちしています。画面の3つの選択肢から回答してください。"


class DecisionLoop:
    def __init__(self, service: ToolService) -> None:
        self._service = service

    async def drive(
        self,
        run: _Run,
        request: str,
        history: tuple[Json, ...],
        before_execute: Callable[[], Awaitable[None]] | None,
    ) -> ToolMaterial:
        if run.confirmation is not None:
            if (
                confirmation_resume_id(run.context.character_id, run.context.session_id)
                != run.confirmation
            ):
                raise MCPFailure("policy", "explicit_confirmation_required")
            if run.confirmation_guard is not None:
                await run.confirmation_guard()
            if before_execute is not None:
                await before_execute()
            candidate = run.candidate
            assert candidate is not None
            logger.info("Tool confirmation continuation: stage=gate_enter")
            envelope = await self._service.gate.resume_confirmation(run.confirmation, run.loop)
            logger.info("Tool confirmation continuation: outcome=%s", envelope["outcome"])
            run.confirmation, run.confirmation_guard = None, None
            material = self._accept_envelope(run, candidate, envelope, before_execute)
            if material is not None:
                return material
        # Gateは外部dispatch、こちらは失敗したDecisionを含むLLM判断回数を制限する。
        for _ in range(12):
            if run.cancellation.is_cancelled:
                raise asyncio.CancelledError()
            all_candidates = catalog(
                self._service.gate,
                run.loop,
                self._service.sanitizer,
                binding_possible=lambda c, o: bool(
                    self._service.bindings.candidates(run.context.character_id, c, o)
                ),
            )
            candidates = select_candidates(
                all_candidates,
                " ".join(
                    [
                        run.original_request,
                        *[
                            q["answer"]
                            for q in run.clarification
                            if q["answer"] is not None
                        ],
                        request,
                    ]
                ),
                preferred_request=request if run.user_followup else "",
            )
            if run.binding_candidate is not None:
                candidates = (run.binding_candidate,)
            elif run.interaction is not None and run.candidate is not None:
                candidates = (run.candidate,)
            if not candidates and not run.interaction:
                return self._service._material(
                    run,
                    "利用できる外部ツールの候補がありません。"
                    if all_candidates
                    else None,
                )
            projections = []
            for candidate in candidates:
                item = candidate.projection()
                item["bindings"] = [
                    {
                        "id": t.id,
                        "label": self._service.sanitizer.text(t.label),
                        "argument_reference": t.argument_reference,
                        # 値はCoreだけが保持する。schemaを改変せず、補完可能な引数名を示す。
                        "provided_arguments": [
                            self._service.sanitizer.text(key, maximum=200)
                            for key in list(json.loads(t.arguments_json))[:128]
                        ],
                    }
                    for t in self._service.bindings.candidates(
                        run.context.character_id, candidate.connection_id, candidate.ref
                    )
                ]
                projections.append(item)
            pending: Json | None = None
            if run.interaction:
                pending = {
                    "answer_schema": run.answer_schema,
                    "questions": run.requests,
                }
            elif run.binding_candidate:
                pending = {
                    "kind": "binding",
                    "instruction": "元の操作を維持し、回答が明確ならcallとbindingを選ぶ",
                }
            decision = await self._service.router.decide(
                {
                    "original_request": run.original_request,
                    "current_user": request,
                    "user_followup": run.user_followup,
                    "history": list(history[-8:]),
                    "clarification": [dict(q) for q in run.clarification],
                    "candidates": projections,
                    "results": run.results,
                    "pending": pending,
                    # 非信頼の結果本文だけではrefreshを解禁しない。
                    "allow_refresh": bool(_REFRESH.search(run.original_request)),
                },
                run.cancellation,
            )
            if run.cancellation.is_cancelled:
                raise asyncio.CancelledError()
            waiting_candidate = run.binding_candidate or (
                run.candidate if run.interaction or run.confirmation else None
            )
            if (
                waiting_candidate is not None
                and waiting_candidate.connection_id
                not in self._service.gate.catalog_snapshots(run.loop)
            ):
                # 追加質問の生成中にOFF→ONされても、古い回答待ちを新規作成しない。
                return self._service._material(
                    run,
                    "連携の利用設定が変わったため、回答待ちを終了しました。必要なら改めて依頼してください。",
                )
            logger.info(
                "Tool decision: action=%s pending=%s followup=%s",
                decision.action,
                run.interaction is not None,
                run.user_followup,
            )
            if decision.action == "finish":
                return self._service._material(run)
            if decision.action == "blocked":
                return self._service._material(
                    run,
                    "追加の許可または管理設定が必要なため、外部操作を停止しました。秘密情報は会話へ入力しないでください。",
                )
            if decision.action == "abandon":
                if not run.user_followup:
                    raise MCPFailure("policy", "unexpected_new_request")
                # 新しいユーザー要求への切替だけを新しい予算の根拠にする。
                self._service.gate.end_loop(run.loop)
                run.context = replace(run.context, action_scope=str(uuid4()))
                run.loop = self._service.gate.begin_loop(run.context)
                run.original_request, run.cycle, run.user_followup = request, 1, False
                run.interaction, run.answer_schema, run.binding_candidate = (
                    None,
                    None,
                    None,
                )
                run.clarification.clear()
                run.results.clear()
                run.sources.clear()
                run.forbidden.clear()
                run.completed_calls.clear()
                continue
            if decision.action == "clarify":
                # MRTRは正本の質問を使い、モデルのメタ説明へ置き換えない。
                text = (
                    "追加情報を教えてください。\n" + "\n".join(run.requests.values())
                    if run.interaction
                    else self._service.sanitizer.text(decision.instruction, maximum=800)
                )
                if not text or _PROTECTED_INPUT.search(text):
                    return self._service._material(
                        run,
                        "管理設定が必要なため、外部操作を停止しました。秘密情報は会話へ入力しないでください。",
                    )
                if run.interaction is None and run.binding_candidate is None:
                    if len(run.clarification) >= 4:
                        return self._service._material(
                            run,
                            "追加回答から実行条件を確定できませんでした。確認を終了します。"
                            "対象と条件をまとめて、改めて依頼してください。",
                        )
                    run.clarification.append({"question": text, "answer": None})
                return self._service._material(run, text, waiting=True)
            if decision.action == "resume":
                if run.interaction is None or run.answer_schema is None:
                    raise MCPFailure("validation", "unexpected_resume")
                answers = parse_object(decision.input_response_json)
                validate_arguments(run.answer_schema, answers)
                if not self._service.sanitizer.arguments_allowed(answers):
                    return self._service._material(
                        run,
                        "安全に送信できない入力が含まれるため、操作を停止しました。",
                    )
                if before_execute is not None:
                    await before_execute()
                envelope = await self._service.gate.resume(run.interaction, answers, run.loop)
                resumed_candidate = run.candidate
                run.interaction = None
                run.answer_schema = None
                run.user_followup = False
                assert resumed_candidate is not None
                candidate = resumed_candidate
            else:
                selected = next(
                    (c for c in candidates if c.id == decision.candidate_id), None
                )
                if selected is None or selected.id in run.forbidden:
                    raise MCPFailure("policy", "invalid_candidate")
                candidate = selected
                if decision.action == "refresh":
                    if not _REFRESH.search(run.original_request) or run.cycle >= 3:
                        raise MCPFailure("policy", "refresh_not_allowed")
                    if before_execute is not None:
                        await before_execute()
                    await self._service.gate.refresh_for_loop(candidate.connection_id, run.loop)
                    self._service.gate.end_loop(run.loop)
                    run.cycle += 1
                    run.loop = self._service.gate.begin_loop(run.context, auto_cycle=run.cycle)
                    continue
                if decision.action != "call" or run.interaction:
                    raise MCPFailure("validation", "invalid_decision")
                arguments = (
                    run.binding_arguments
                    if run.binding_candidate
                    else {}
                    if candidate.kind == "resource"
                    else decision.arguments()
                )
                try:
                    binding_id, constraints = self._service.bindings.resolve(
                        run.context.character_id,
                        run.context.session_id,
                        candidate.connection_id,
                        candidate.ref,
                        explicit=decision.binding,
                        required=candidate.binding_required,
                    )
                except MCPFailure as error:
                    if error.code != "binding_input_required":
                        raise
                    run.binding_candidate, run.binding_arguments = candidate, arguments
                    labels = [
                        self._service.sanitizer.text(t.label)
                        for t in self._service.bindings.candidates(
                            run.context.character_id,
                            candidate.connection_id,
                            candidate.ref,
                        )
                    ]
                    return self._service._material(
                        run,
                        "操作対象を指定してください：" + "、".join(labels),
                        waiting=True,
                    )
                run.binding_candidate = None
                run.user_followup = False
                arguments = apply_binding(
                    arguments, constraints,
                    reference=self._service.bindings.argument_reference(binding_id),
                )
                if not self._service.sanitizer.arguments_allowed(arguments):
                    return self._service._material(
                        run,
                        "安全に送信できない入力が含まれるため、操作を停止しました。秘密情報は会話へ入力しないでください。",
                    )
                fingerprint = digest(
                    {
                        "candidate": candidate.id,
                        "binding": binding_id,
                        "arguments": arguments,
                    }
                )
                if fingerprint in run.completed_calls:
                    # 成功済み操作の同一再実行をモデルの反復で増やさない。
                    return self._service._material(run)
                run.call_fingerprint = fingerprint
                if candidate.may_change_state:
                    self._protect_core(arguments)
                run.binding_id = binding_id
                if before_execute is not None:
                    await before_execute()
                if candidate.kind == "tool":
                    envelope = await self._service.gate.invoke(
                        candidate.connection_id,
                        candidate.ref,
                        arguments,
                        run.loop,
                        binding_id=binding_id,
                    )
                else:
                    if arguments:
                        raise MCPFailure("validation", "resource_has_no_arguments")
                    envelope = await self._service.gate.read_resource(
                        candidate.connection_id,
                        candidate.ref,
                        run.loop,
                        binding_id=binding_id,
                    )
            if run.cancellation.is_cancelled:
                raise asyncio.CancelledError()
            logger.info(
                "Tool result: outcome=%s category=%s",
                envelope["outcome"],
                envelope.get("error_category", "none"),
            )
            material = self._accept_envelope(run, candidate, envelope, before_execute)
            if material is not None:
                return material
        return self._service._material(
            run,
            "外部ツールの判断回数が上限に達しました。取得できていない内容は回答できません。",
        )

    def _accept_envelope(
        self,
        run: _Run,
        candidate: Candidate,
        envelope: Json,
        before_execute: Callable[[], Awaitable[None]] | None,
    ) -> ToolMaterial | None:
        if envelope["outcome"] == "confirmation_required":
            run.confirmation = envelope["confirmation_id"]
            run.candidate, run.confirmation_guard = candidate, before_execute
            return self._service._material(
                run,
                CONFIRMATION_REQUIRED_MESSAGE,
                waiting=True,
            )
        if envelope["outcome"] == "input_required":
            run.interaction = envelope["interaction_id"]
            run.candidate = candidate
            try:
                run.answer_schema, run.requests = self._interaction(envelope)
            except MCPFailure:
                return self._service._material(
                    run,
                    "追加の許可または管理設定が必要なため、操作を停止しました。秘密情報は会話へ入力しないでください。",
                )
            # 現在contextで答えられる場合は次の判断でresume、足りなければ質問。
            return None
        source = {
            "label": candidate.name,
            "source_id": candidate.id,
            "snapshot_revision": candidate.snapshot_revision,
            "binding_id": run.binding_id,
            "connection_instance_id": candidate.connection_id,
        }
        result = self._service.sanitizer.result(
            envelope, may_change_state=candidate.may_change_state
        )
        # native結果の自己申告ではなく、固定snapshotの実行分類を判断側へ渡す。
        result["operation_effect"] = (
            "may_change_state" if candidate.may_change_state else "read_only"
        )
        result["source"] = {"label": candidate.name, "source_id": candidate.id}
        # 最終の未完了理由にも枠を残し、部分成功を回答へ統合できるようにする。
        remaining = 3_584 - sum(len(encode(r).encode()) for r in run.results)
        if remaining >= 128:
            run.results.append(json.loads(bounded_json(result, remaining)))
        else:
            return self._service._material(
                run,
                "取得結果が会話の上限に達しました。対象を絞って依頼してください。",
            )
        if envelope["outcome"] in {"succeeded", "no_change"}:
            run.sources.append(source)
            if run.call_fingerprint is not None:
                run.completed_calls.add(run.call_fingerprint)
        if result["outcome"] in {
            "budget_exceeded",
            "running",
            "result_unknown",
            "cancelled",
        }:
            run.forbidden.add(candidate.id)
            return self._service._material(run)
        if envelope.get("error_category") == "validation" and any(
            s["source_id"] == candidate.id for s in run.sources
        ):
            # 完了済み操作の再呼出しを、引数修復の連鎖で繰り返さない。
            return self._service._material(run)
        if (
            envelope["outcome"] not in {"succeeded", "no_change"}
            and not (
                envelope["outcome"] == "conflict"
                and "latest_state" in result.get("structured", {})
            )
            and envelope.get("error_category") != "validation"
        ):
            run.forbidden.add(candidate.id)
        if envelope["outcome"] in {"rejected", "deferred", "cancel_requested"}:
            return self._service._material(run)
        return None

    def _protect_core(self, arguments: Json) -> None:
        """未知のserver cwdで相対pathを推測せず、明示pathを安全側に判定する。"""

        def check(value: object, key: str = "") -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    check(v, str(k))
            elif isinstance(value, list):
                for v in value:
                    check(v, key)
            elif isinstance(value, str):
                if any(str(root) in value for root in self._service.protected_roots):
                    raise MCPFailure("policy", "core_write_denied")
                path_value = value.removeprefix("file://")
                # key名は任意なので、値のpath表現も検査する。URLはファイルpathではない。
                if "://" in path_value:
                    return
                if (
                    re.search(r"path|file|directory|root|repo|database", key, re.I)
                    or re.search(r"[/\\]", path_value)
                    or path_value.startswith(".")
                ):
                    if not Path(path_value).is_absolute():
                        raise MCPFailure("policy", "relative_write_path_denied")
                    path = Path(path_value).resolve()
                    if any(
                        path == root or root in path.parents or path in root.parents
                        for root in self._service.protected_roots
                    ):
                        raise MCPFailure("policy", "core_write_denied")

        check(arguments)

    def _interaction(self, envelope: Json) -> tuple[Json, Json]:
        native = envelope.get("native_payload")
        requests = native.get("inputRequests") if isinstance(native, dict) else None
        if not isinstance(requests, dict) or not 1 <= len(requests) <= 4:
            raise MCPFailure("policy", "unsupported_input")
        properties: Json = {}
        questions: Json = {}
        for request_id, item in requests.items():
            if not isinstance(item, dict) or not isinstance(item.get("params"), dict):
                raise MCPFailure("policy", "unsupported_input")
            params = item.get("params") or {}
            schema = params.get("requestedSchema")
            if (
                item.get("method") != "elicitation/create"
                or params.get("mode", "form") != "form"
                or not isinstance(schema, dict)
                or not isinstance(params.get("message", ""), str)
                or _PROTECTED_INPUT.search(encode(params))
                or self._service.sanitizer.text(request_id) != request_id
                or _PROTECTED_INPUT.search(request_id)
            ):
                raise MCPFailure("policy", "protected_input")
            serialized = encode(schema)
            self._validate_interaction_schema(schema)
            scan = self._service.sanitizer.scanner.scan(serialized)
            if (
                not isinstance(scan, ScanSuccess)
                or scan.findings
                or any(v and v in serialized for v in self._service.sanitizer.sensitive_values)
            ):
                raise MCPFailure("policy", "protected_input")
            # schemaにも外部の命令・秘密を持ち込ませない。正本の検証はGateに加えてここで行う。
            properties[request_id] = {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "content"],
                "properties": {"action": {"const": "accept"}, "content": schema},
            }
            questions[request_id] = self._service.sanitizer.text(
                params.get("message", "追加情報が必要です。"), maximum=800
            )
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }, questions

    @staticmethod
    def _validate_interaction_schema(schema: Json) -> None:
        """追加質問schemaの再帰・参照・正規表現を、同期validatorへ渡す前に制限する。"""
        if len(encode(schema).encode()) > 16_384:
            raise MCPFailure("policy", "unsupported_input_schema")
        patterns = 0
        pending: list[tuple[object, int]] = [(schema, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 16:
                raise MCPFailure("policy", "unsupported_input_schema")
            if isinstance(value, dict):
                if "$ref" in value or "$dynamicRef" in value:
                    raise MCPFailure("policy", "unsupported_input_schema")
                for key, item in value.items():
                    expressions = (
                        [item]
                        if key == "pattern"
                        else list(item)
                        if key == "patternProperties" and isinstance(item, dict)
                        else []
                    )
                    for expression in expressions:
                        patterns += 1
                        # 初期の追加質問では固定長の文字・文字クラスのみ許可する。
                        # 複雑な正規表現を縮約して元schemaと異なる入力を許可しない。
                        if (
                            patterns > 16
                            or not isinstance(expression, str)
                            or len(expression) > 128
                            or re.search(r"[()*+?{}|\\\\]", expression)
                        ):
                            raise MCPFailure("policy", "unsupported_input_schema")
                    pending.append((item, depth + 1))
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
        input_validator(schema)
