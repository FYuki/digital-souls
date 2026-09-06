"""loopに固定されたnative definitionからLLM選択用indexを作る。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Literal

from app.external_mcp import ExecutionGate
from app.external_mcp.models import Json, digest, encode
from app.privacy.contracts import ScanSuccess
from .projection import Sanitizer


@dataclass(frozen=True)
class Candidate:
    id: str
    connection_id: str
    ref: str = field(repr=False)
    kind: Literal["tool", "resource"]
    snapshot_revision: str
    name: str
    description: str
    schema_json: str = field(repr=False)
    binding_required: bool
    may_change_state: bool
    source_label: str

    def projection(self) -> Json:
        import json

        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "source": self.source_label,
            "description": self.description,
            "input_schema": json.loads(self.schema_json),
            "binding_required": self.binding_required,
        }


def catalog(
    gate: ExecutionGate,
    loop_id: str,
    sanitizer: Sanitizer,
    *,
    binding_possible: Callable[[str, str], bool] = lambda _c, _o: True,
) -> tuple[Candidate, ...]:
    result: list[Candidate] = []
    for connection_id, snapshot in gate.catalog_snapshots(loop_id).items():
        connection = gate.registry.entry(connection_id).connection
        required = connection.manifest["core_policy"]["resource_binding_required"]
        for kind, definitions in (
            ("tool", snapshot.document["tools"]),
            ("resource", snapshot.document["resources"]),
        ):
            for definition in definitions:
                ref = definition["name" if kind == "tool" else "uri"]
                if definition.get("status", "active") != "active":
                    continue
                if "deny" in connection.restrictions(ref):
                    continue
                if required and not binding_possible(connection_id, ref):
                    continue
                native = definition.get("native_definition", definition)
                # schemaは縮約せず正本から複製する。秘密を含むschemaは選択対象外。
                schema = definition.get(
                    "input_schema", {"type": "object", "properties": {}}
                )
                schema_text = encode(schema)
                if any(v and v in schema_text for v in sanitizer.sensitive_values):
                    continue
                scan = sanitizer.scanner.scan(schema_text)
                if not isinstance(scan, ScanSuccess) or scan.findings:
                    continue
                result.append(
                    Candidate(
                        id=digest([connection_id, kind, ref, snapshot.revision])[7:31],
                        connection_id=connection_id,
                        ref=ref,
                        kind="tool" if kind == "tool" else "resource",
                        snapshot_revision=snapshot.revision,
                        name=sanitizer.text(
                            native.get("name", "外部資料"), maximum=160
                        ),
                        description=sanitizer.text(
                            native.get("description", ""), maximum=2_000
                        ),
                        schema_json=schema_text,
                        binding_required=required,
                        may_change_state=(
                            kind == "tool"
                            and definition["effective_policy"]["effect"] != "read"
                        ),
                        source_label=sanitizer.text(connection_id, maximum=160),
                    )
                )
    return tuple(result)


def _terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_]+", text.lower()))
    words.update(part for word in tuple(words) for part in word.split("_") if part)
    for japanese, english in (
        ("ファイル", "file files"),
        ("読ん", "read text"),
        ("読む", "read text"),
        ("資料", "document resource"),
        ("検索", "search"),
        ("一覧", "list"),
    ):
        if japanese in text:
            words.update(english.split())
    # 日本語の複合語を形態素辞書に依存せず関連づける。
    for run in re.findall(r"[\u3040-\u30ff\u3400-\u9fff]+", text):
        words.update(run[i : i + 2] for i in range(len(run) - 1))
    return words


def select_candidates(
    candidates: tuple[Candidate, ...],
    request: str,
    *,
    maximum: int = 8,
    schema_budget: int = 4_096,
) -> tuple[Candidate, ...]:
    terms = _terms(request)

    def relevance(candidate: Candidate) -> int:
        # 利用者が挙げた資料名を一般的な「read」「file」より優先する。
        exact = bool(candidate.name and candidate.name.lower() in request.lower())
        return (
            (100 if exact else 0)
            + 3 * len(terms & _terms(candidate.name))
            + len(terms & _terms(candidate.description))
        )

    ranked = sorted(
        candidates,
        key=lambda c: (-relevance(c), c.id),
    )
    selected: list[Candidate] = []
    size = 2
    for candidate in ranked:
        cost = len(encode(candidate.projection()).encode()) + 1
        if size + cost > schema_budget:
            continue
        selected.append(candidate)
        size += cost
        if len(selected) == maximum:
            break
    return tuple(selected)
