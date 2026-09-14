"""検索に使った記憶の版と回答発言の関係。本文を保存しない。"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable
import json
import sqlite3
from uuid import UUID

from app.memory.episodic.contracts import SourceSpan
from app.memory.episodic.source_masks import overlaps_mask

TABLES = frozenset({"memory_response_origins", "memory_response_dependencies"})
DEFINITIONS = (
    """CREATE TABLE memory_response_origins (
        character_id TEXT NOT NULL CHECK(length(trim(character_id)) > 0),
        conversation_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(character_id,turn_id)
    )""",
    """CREATE TABLE memory_response_dependencies (
        character_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        memory_id TEXT NOT NULL,
        content_version INTEGER NOT NULL CHECK(content_version > 0),
        PRIMARY KEY(character_id,turn_id,memory_id,content_version),
        FOREIGN KEY(character_id,turn_id)
          REFERENCES memory_response_origins(character_id,turn_id) ON DELETE CASCADE
    )""",
    """CREATE INDEX idx_memory_response_dependency_target
       ON memory_response_dependencies(character_id,memory_id,content_version)""",
)
MemoryVersion = tuple[str, int]
SourceValidator = Callable[[UUID | None, tuple[SourceSpan, ...]], bool]


def initialize_schema(connection: sqlite3.Connection) -> None:
    for definition in DEFINITIONS:
        connection.execute(definition)


def validate_schema(connection: sqlite3.Connection) -> None:
    expected = sqlite3.connect(":memory:")
    try:
        initialize_schema(expected)
        for kind, name, definition in expected.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL"
        ):
            actual = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)
            ).fetchone()
            if actual is None or " ".join(actual[0].split()) != " ".join(definition.split()):
                raise ValueError("response provenance schema is invalid")
    finally:
        expected.close()


def record_response(
    connection: sqlite3.Connection, *, character_id: str, conversation_id: UUID,
    turn_id: UUID, references: Iterable[tuple[UUID, int]],
    history_turn_ids: Iterable[UUID] = (), created_at: str,
) -> None:
    """回答保存前に確定する。履歴として渡した回答の依存版も引き継ぐ。"""
    if not character_id.strip() or conversation_id.version != 4 or turn_id.version != 4:
        raise ValueError("response provenance identity is invalid")
    previous = connection.execute(
        "SELECT conversation_id FROM memory_response_origins WHERE character_id=? AND turn_id=?",
        (character_id, str(turn_id)),
    ).fetchone()
    if previous is not None and previous[0] != str(conversation_id):
        raise ValueError("response provenance conversation is immutable")
    dependencies: set[MemoryVersion] = set()
    for memory_id, version in references:
        if memory_id.version != 4 or type(version) is not int or version <= 0:
            raise ValueError("memory reference version is invalid")
        # 参照採用後に訂正されても旧版の依存を記録する。別characterのIDは受け付けない。
        exists = connection.execute(
            """SELECT 1 FROM episodic_versions WHERE character_id=? AND record_id=? AND content_version=?
               UNION ALL SELECT 1 FROM approved_memories WHERE character_id=? AND id=?
               AND content_version>=?
               UNION ALL SELECT 1 FROM semantic_versions WHERE character_id=? AND record_id=? AND content_version=?""",
            (character_id, str(memory_id), version, character_id, str(memory_id), version,
             character_id, str(memory_id), version),
        ).fetchone()
        if exists is None:
            raise ValueError("memory reference is outside the character or unavailable")
        dependencies.add((str(memory_id), version))
    for parent_id in history_turn_ids:
        if parent_id.version != 4 or parent_id == turn_id:
            raise ValueError("history response identity is invalid")
        parent = connection.execute(
            "SELECT conversation_id FROM memory_response_origins WHERE character_id=? AND turn_id=?",
            (character_id, str(parent_id)),
        ).fetchone()
        if parent is not None and parent[0] != str(conversation_id):
            raise ValueError("prompt history belongs to another conversation")
        dependencies.update((str(row[0]), int(row[1])) for row in connection.execute(
            "SELECT memory_id,content_version FROM memory_response_dependencies WHERE character_id=? AND turn_id=?",
            (character_id, str(parent_id)),
        ))
    connection.execute(
        "INSERT OR IGNORE INTO memory_response_origins VALUES (?,?,?,?)",
        (character_id, str(conversation_id), str(turn_id), created_at),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO memory_response_dependencies VALUES (?,?,?,?)",
        [(character_id, str(turn_id), memory_id, version)
         for memory_id, version in sorted(dependencies)],
    )


def invalid_provenance(
    connection: sqlite3.Connection, character_id: str, *,
    masks: tuple[SourceSpan, ...] = (), source_validator: SourceValidator | None = None,
) -> tuple[set[UUID], set[UUID]]:
    """版付き依存をたどり、無効な回答と旧preferenceを求める。"""
    by_turn: dict[str, set[MemoryVersion]] = defaultdict(set)
    for turn_id, memory_id, version in connection.execute(
        "SELECT turn_id,memory_id,content_version FROM memory_response_dependencies WHERE character_id=?",
        (character_id,),
    ):
        by_turn[str(turn_id)].add((str(memory_id), int(version)))
    dependencies: dict[MemoryVersion, set[MemoryVersion]] = defaultdict(set)
    eligible: set[MemoryVersion] = set()
    invalid_sources = {(str(row[0]), int(row[1])) for row in connection.execute(
        "SELECT source_id,revision FROM episodic_invalid_sources WHERE character_id=?",
        (character_id,),
    )}
    for record_id, version, status, current, content, sources_json, conversation_id in connection.execute(
        """SELECT v.record_id,v.content_version,r.status,r.content_version,v.content,v.sources,r.conversation_id
           FROM episodic_versions v JOIN episodic_records r
           ON r.character_id=v.character_id AND r.id=v.record_id WHERE v.character_id=?""",
        (character_id,),
    ):
        node = str(record_id), int(version)
        sources = tuple(SourceSpan.model_validate(s) for s in json.loads(sources_json))
        if status == "ACTIVE" and version == current and content is not None and not any(
            (str(s.source_id), s.revision) in invalid_sources or overlaps_mask(s, masks)
            for s in sources
        ):
            if source_validator is None or source_validator(
                UUID(conversation_id) if conversation_id else None, sources,
            ):
                eligible.add(node)
        for source in sources:
            if source.role == "assistant":
                dependencies[node].update(by_turn[str(source.source_id)])
    # Semanticも回答の版付き依存に加える。Episode/Factと同じ有効性の固定点で循環を拒否する。
    from app.memory.semantic.contracts import SemanticSource
    for record_id, version, status, current, content, sources_json, formation_type in connection.execute(
        """SELECT v.record_id,v.content_version,r.status,r.content_version,v.proposition,v.sources,r.formation_type
           FROM semantic_versions v JOIN semantic_records r
           ON r.character_id=v.character_id AND r.id=v.record_id WHERE v.character_id=?""",
        (character_id,),
    ):
        node = str(record_id), int(version)
        semantic_sources = tuple(SemanticSource.model_validate(s) for s in json.loads(sources_json))
        direct_confirmation = formation_type == "DIRECT_EXTRACTION" and any(
            source.kind == "MANUAL" or (source.span is not None and source.span.role == "user")
            for source in semantic_sources
        )
        safe = (status in {"ACTIVE", "HISTORICAL"} and version == current and content is not None
                and (formation_type != "DIRECT_EXTRACTION" or direct_confirmation))
        for semantic_source in semantic_sources:
            if semantic_source.kind == "EPISODE":
                dependencies[node].add((str(semantic_source.source_id), semantic_source.revision))
            elif semantic_source.kind == "CONVERSATION":
                span = semantic_source.span
                assert span is not None
                if source_validator is not None and not source_validator(semantic_source.conversation_id, (span,)):
                    safe = False
                # 直接取得の権威は新しいユーザー申告・明示確認にある。assistant引用は対象解釈の文脈。
                # 引用発言自体の版は上で検証するが、その発言が参照した古い記憶版を継承しない。
                if span.role == "assistant" and not direct_confirmation:
                    dependencies[node].update(by_turn[str(span.source_id)])
        if safe:
            eligible.add(node)
    legacy_nodes: set[MemoryVersion] = set()
    removed_turns = {str(source.source_id) for source in masks}
    removed_turns.update(source_id for source_id, _ in invalid_sources)
    legacy_sources = legacy_source_turns(connection, character_id)
    for memory_id, version, status in connection.execute(
        "SELECT id,content_version,status FROM approved_memories WHERE character_id=?", (character_id,),
    ):
        node = str(memory_id), int(version)
        legacy_nodes.add(node)
        turns = legacy_sources[node[0]]
        if status == "ACTIVE" and not turns & removed_turns:
            eligible.add(node)
        for turn_id in turns:
            dependencies[node].update(by_turn[turn_id])
    for episode_id, episode_version, fact_id, fact_version, valid in connection.execute(
        "SELECT episode_id,episode_version,fact_id,fact_version,valid FROM episodic_links WHERE character_id=?",
        (character_id,),
    ):
        node = str(episode_id), int(episode_version)
        dependencies[node].add((str(fact_id), int(fact_version)))
        if not valid:
            eligible.discard(node)
    for source_id, source_version, target_id, target_version, valid in connection.execute(
        "SELECT source_fact_id,source_version,target_fact_id,target_version,valid FROM episodic_merges WHERE character_id=?",
        (character_id,),
    ):
        node = str(source_id), int(source_version)
        dependencies[node].add((str(target_id), int(target_version)))
        if not valid:
            eligible.discard(node)
    # 依存先が全て検証済みになった節点だけを有効化する。循環は有効化されない。
    waiting = {node: len(dependencies[node]) for node in eligible}
    reverse: dict[MemoryVersion, set[MemoryVersion]] = defaultdict(set)
    for node in eligible:
        for dependency in dependencies[node]:
            reverse[dependency].add(node)
    pending = deque(node for node, count in waiting.items() if count == 0)
    valid_nodes: set[MemoryVersion] = set()
    while pending:
        node = pending.popleft()
        valid_nodes.add(node)
        for dependent in reverse[node]:
            waiting[dependent] -= 1
            if waiting[dependent] == 0:
                pending.append(dependent)
    return (
        {UUID(turn_id) for turn_id, refs in by_turn.items() if not refs <= valid_nodes},
        {UUID(memory_id) for memory_id, version in legacy_nodes if (memory_id, version) not in valid_nodes},
    )


def invalid_responses(
    connection: sqlite3.Connection, character_id: str, *, masks: tuple[SourceSpan, ...] = (),
    source_validator: SourceValidator | None = None,
) -> set[UUID]:
    return invalid_provenance(connection, character_id, masks=masks, source_validator=source_validator)[0]


def legacy_source_turns(
    connection: sqlite3.Connection, character_id: str, *, include_superseded: bool = False,
) -> dict[str, set[str]]:
    """旧記憶の会話出典。最新の明示的な手動訂正だけを独立した根拠として扱う。"""
    sources: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for memory_id, kind, ref in connection.execute(
        "SELECT memory_id,source_type,source_ref FROM memory_sources WHERE character_id=?", (character_id,),
    ):
        sources[str(memory_id)].append((str(kind), str(ref)))
    result: dict[str, set[str]] = {}
    parents: dict[str, set[str]] = defaultdict(set)
    independent: set[str] = set()
    for memory_id, write_key in connection.execute(
        "SELECT id,last_write_idempotency_key FROM approved_memories WHERE character_id=?", (character_id,),
    ):
        evidence = sources[str(memory_id)]
        result[str(memory_id)] = set()
        if not include_superseded and any(kind == "USER_CORRECTION" and ref == write_key for kind, ref in evidence):
            independent.add(str(memory_id))
            continue
        for kind, ref in evidence:
            if kind == "CONSOLIDATION":
                parents[str(memory_id)].add(ref)
            if kind != "CONVERSATION_TURN":
                continue
            turn_id = conversation_source_id(ref)
            if turn_id is not None:
                result[str(memory_id)].add(str(turn_id))
    for memory_id, related_id in connection.execute(
        "SELECT memory_id,related_memory_id FROM memory_lineage WHERE character_id=?", (character_id,),
    ):
        parents[str(memory_id)].add(str(related_id))
    # 旧consolidationは入力版を持たない。親を手動訂正しただけで既存の子の
    # 旧出典を消し、独立した新しい根拠として扱わない。子自身の手動訂正は上で分離する。
    historical_parents = (
        {} if include_superseded else legacy_source_turns(connection, character_id, include_superseded=True)
    )
    changed = True
    while changed:
        changed = False
        for memory_id, related_ids in parents.items():
            if memory_id not in result or memory_id in independent:
                continue
            before = len(result[memory_id])
            for related_id in related_ids:
                result[memory_id].update(historical_parents.get(related_id, result.get(related_id, set())))
            changed |= len(result[memory_id]) != before
    return result


def conversation_source_id(source_ref: str) -> UUID | None:
    try:
        turn_id = UUID(source_ref.rsplit(":", 1)[-1])
    except ValueError:
        return None
    return turn_id if turn_id.version == 4 else None
