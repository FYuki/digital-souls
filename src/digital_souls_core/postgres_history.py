"""PostgreSQL history adapter sharing the atomic memory revocation boundary."""

import json
from contextlib import AbstractContextManager
from typing import Any
from uuid import uuid4

from psycopg import Connection

from .application import CoreError
from .contracts import Message
from .history import (
    Binding,
    Clock,
    ConversationControls,
    MemoryConfirmation,
    Receipt,
    Snapshot,
    SourceDeletion,
    SourceReference,
    SourceState,
    TurnDeletion,
    TurnDeletionInput,
    TurnDeletionResult,
    TurnMetadata,
    as_utc,
    current_utc,
)
from .memory_confirmation import blocked, decode, pending, resolve
from .postgres_db import PostgresDatabase
from .postgres_db import key as _key
from .postgres_schema import revoke, revoke_turns
from .turn_deletion import select_turns


class PostgresHistory:
    def __init__(self, database: PostgresDatabase, *, clock: Clock = current_utc) -> None:
        self._clock = clock
        self.database = database
        database.initialize()

    def _connection(self, binding: Binding) -> AbstractContextManager[Connection[tuple[Any, ...]]]:
        return self.database.transaction(binding)

    def create(self, binding: Binding) -> Snapshot:
        identifier = str(uuid4())
        with self._connection(binding) as db:
            db.execute(
                "INSERT INTO conversations (binding,id,revision) VALUES (%s, %s, 0)",
                (_key(binding), identifier),
            )
        return Snapshot(identifier, 0, ())

    def list(self, binding: Binding, *, include_archived: bool = False) -> list[str]:
        with self._connection(binding) as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT id FROM conversations WHERE binding=%s "
                    "AND (archived=false OR %s) ORDER BY seq",
                    (_key(binding), include_archived),
                )
            ]

    def read(self, binding: Binding, conversation_id: str) -> Snapshot:
        with self._connection(binding) as db:
            row = db.execute(
                "SELECT revision,private_mode,archived FROM conversations "
                "WHERE binding=%s AND id=%s",
                (_key(binding), conversation_id),
            ).fetchone()
            if row is None:
                raise CoreError(404, "conversation_not_found", "Unknown conversation")
            messages: list[Message] = []
            sources: list[SourceState] = []
            confirmations: list[SourceReference] = []
            turns: list[TurnMetadata] = []
            for turn in db.execute(
                "SELECT messages,revision,memory_excluded,private_mode,stated_at,"
                "memory_confirmation,card_sha256 FROM turns "
                "WHERE binding=%s AND conversation=%s ORDER BY revision",
                (_key(binding), conversation_id),
            ):
                turns.append(TurnMetadata(turn[1], turn[6]))
                excluded = json.loads(turn[2])
                state = decode(turn[5])
                confirmations.extend(
                    SourceReference(conversation_id, turn[1], int(index))
                    for index, answer in state.items()
                    if answer is None
                )
                for index, value in enumerate(json.loads(turn[0])):
                    messages.append(Message.model_validate(value))
                    sources.append(
                        SourceState(
                            SourceReference(conversation_id, turn[1], index),
                            not row[1]
                            and not turn[3]
                            and index not in excluded
                            and not blocked(state, index),
                            as_utc(turn[4]) if turn[4] is not None else None,
                        )
                    )
            return Snapshot(
                conversation_id,
                row[0],
                tuple(messages),
                bool(row[1]),
                bool(row[2]),
                tuple(sources),
                tuple(confirmations),
                tuple(turns),
            )

    def receipt(self, binding: Binding, conversation_id: str, request_id: str) -> Receipt | None:
        with self._connection(binding) as db:
            return self._receipt(db, binding, conversation_id, request_id)

    def _receipt(
        self,
        db: Connection[tuple[Any, ...]],
        binding: Binding,
        conversation_id: str,
        request_id: str,
    ) -> Receipt | None:
        if db.execute(
            "SELECT 1 FROM turn_tombstones WHERE binding=%s AND conversation=%s AND request=%s",
            (_key(binding), conversation_id, request_id),
        ).fetchone():
            raise CoreError(409, "request_deleted", "Request was deleted")
        row = db.execute(
            "SELECT fingerprint, revision, messages, finish, memory_confirmation,card_sha256 "
            "FROM turns "
            "WHERE binding=%s AND conversation=%s AND request=%s",
            (_key(binding), conversation_id, request_id),
        ).fetchone()
        if row is None:
            return None
        return Receipt(
            row[0],
            row[1],
            Message.model_validate(json.loads(row[2])[-1]),
            row[3],
            tuple(int(index) for index in decode(row[4])),
            row[5],
        )

    def append(
        self,
        binding: Binding,
        conversation_id: str,
        request_id: str,
        fingerprint: str,
        expected_revision: int,
        messages: tuple[Message, ...],
        finish_reason: str,
        *,
        memory_excluded_indices: tuple[int, ...] = (),
        memory_confirmation_indices: tuple[int, ...] = (),
        card_sha256: str | None = None,
    ) -> Receipt:
        with self._connection(binding) as db:
            prior = self._receipt(db, binding, conversation_id, request_id)
            if prior:
                if prior.fingerprint != fingerprint:
                    raise CoreError(409, "request_conflict", "Request id has different input")
                return prior
            cursor = db.execute(
                "UPDATE conversations SET revision=revision+1 "
                "WHERE binding=%s AND id=%s AND revision=%s",
                (_key(binding), conversation_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise CoreError(409, "revision_conflict", "Conversation changed or was deleted")
            if len(set(memory_excluded_indices)) != len(memory_excluded_indices) or any(
                type(index) is not int or not 0 <= index < len(messages) - 1
                for index in memory_excluded_indices
            ):
                raise CoreError(400, "invalid_exclusions", "Invalid message exclusion indices")
            private_row = db.execute(
                "SELECT private_mode FROM conversations WHERE binding=%s AND id=%s",
                (_key(binding), conversation_id),
            ).fetchone()
            if private_row is None:
                raise CoreError(503, "storage_unavailable", "PostgreSQL storage is unavailable")
            private = private_row[0]
            state = pending(memory_confirmation_indices, messages)
            excluded = list(memory_excluded_indices)
            tainted = bool(excluded) or bool(
                db.execute(
                    "SELECT 1 FROM turns WHERE binding=%s AND conversation=%s "
                    "AND (private_mode=true OR memory_excluded != '[]') LIMIT 1",
                    (_key(binding), conversation_id),
                ).fetchone()
            )
            held = bool(state) or any(
                any(answer is not False for answer in decode(row[0]).values())
                for row in db.execute(
                    "SELECT memory_confirmation FROM turns WHERE binding=%s AND conversation=%s",
                    (_key(binding), conversation_id),
                )
            )
            if tainted or held:
                # Every generated/tool message can depend on the full supplied history.
                # Independent new user input remains eligible unless explicitly excluded.
                excluded = sorted(
                    set(excluded)
                    | {index for index, message in enumerate(messages) if message.role != "user"}
                )
            encoded = json.dumps([m.model_dump(exclude_none=True) for m in messages])
            stated_at = as_utc(self._clock())
            db.execute(
                "INSERT INTO turns (binding,conversation,request,fingerprint,revision,"
                "messages,finish,"
                "memory_excluded,private_mode,stated_at,memory_confirmation,card_sha256) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    _key(binding),
                    conversation_id,
                    request_id,
                    fingerprint,
                    expected_revision + 1,
                    encoded,
                    finish_reason,
                    json.dumps(excluded),
                    private,
                    stated_at,
                    json.dumps(state),
                    card_sha256,
                ),
            )
            return Receipt(
                fingerprint,
                expected_revision + 1,
                messages[-1],
                finish_reason,
                memory_confirmation_indices,
                card_sha256,
            )

    def delete(self, binding: Binding, conversation_id: str) -> None:
        with self._connection(binding) as db:
            row = db.execute(
                "SELECT revision,memory_epoch FROM conversations WHERE binding=%s AND id=%s",
                (_key(binding), conversation_id),
            ).fetchone()
            if row is None:
                return
            db.execute(
                "INSERT INTO source_deletions (event,binding,conversation,through_revision) "
                "VALUES (%s, %s, %s, %s)",
                (str(uuid4()), _key(binding), conversation_id, row[0]),
            )
            revoke(db, _key(binding), conversation_id, row[1] + 1, "delete")
            # Idempotent; event and physical source deletion commit together.
            db.execute(
                "DELETE FROM conversations WHERE binding=%s AND id=%s",
                (_key(binding), conversation_id),
            )

    def delete_turns(
        self, binding: Binding, conversation_id: str, selection: TurnDeletionInput
    ) -> TurnDeletionResult:
        with self._connection(binding) as db:
            key = _key(binding)
            row = db.execute(
                "SELECT revision,memory_epoch FROM conversations WHERE binding=%s AND id=%s",
                (key, conversation_id),
            ).fetchone()
            if row is None:
                raise CoreError(404, "conversation_not_found", "Unknown conversation")
            if row[0] != selection.expected_revision:
                raise CoreError(409, "revision_conflict", "Conversation revision changed")
            turns = {
                revision: tuple(Message.model_validate(m) for m in json.loads(messages))
                for revision, messages in db.execute(
                    "SELECT revision,messages FROM turns WHERE binding=%s AND conversation=%s",
                    (key, conversation_id),
                )
            }
            if selection.turn_revision not in turns:
                raise CoreError(409, "turn_deletion_conflict", "Turn is unavailable")
            revisions = select_turns(turns, selection.turn_revision, selection.scope)
            db.execute(
                "UPDATE conversations SET revision=revision+1 WHERE binding=%s AND id=%s",
                (key, conversation_id),
            )
            db.execute(
                "INSERT INTO turn_tombstones(binding,conversation,request,revision) "
                "SELECT binding,conversation,request,revision FROM turns "
                "WHERE binding=%s AND conversation=%s AND revision=ANY(%s)",
                (key, conversation_id, list(revisions)),
            )
            revoke_turns(db, key, conversation_id, row[1], revisions)
            db.execute(
                "INSERT INTO turn_deletions(event,binding,conversation,turn_revisions) "
                "VALUES (%s,%s,%s,%s)",
                (str(uuid4()), key, conversation_id, json.dumps(revisions)),
            )
            db.execute(
                "DELETE FROM turns WHERE binding=%s AND conversation=%s AND revision=ANY(%s)",
                (key, conversation_id, list(revisions)),
            )
            return TurnDeletionResult(conversation_id, row[0] + 1, revisions)

    def turn_deletions(self, binding: Binding) -> tuple[TurnDeletion, ...]:
        with self._connection(binding) as db:
            return tuple(
                TurnDeletion(event, conversation, tuple(json.loads(revisions)))
                for event, conversation, revisions in db.execute(
                    "SELECT event,conversation,turn_revisions FROM turn_deletions "
                    "WHERE binding=%s ORDER BY seq",
                    (_key(binding),),
                )
            )

    def acknowledge_turn_deletion(self, binding: Binding, event_id: str) -> None:
        with self._connection(binding) as db:
            db.execute(
                "DELETE FROM turn_deletions WHERE binding=%s AND event=%s",
                (_key(binding), event_id),
            )

    def controls(
        self, binding: Binding, conversation_id: str, changes: ConversationControls
    ) -> Snapshot:
        """Change thread state with the same revision CAS as inference commits."""
        with self._connection(binding) as db:
            before = db.execute(
                "SELECT private_mode,memory_epoch FROM conversations WHERE binding=%s AND id=%s",
                (_key(binding), conversation_id),
            ).fetchone()
            result = db.execute(
                "UPDATE conversations SET revision=revision+1, "
                "private_mode=COALESCE(%s,private_mode), archived=COALESCE(%s,archived) "
                "WHERE binding=%s AND id=%s AND revision=%s",
                (
                    changes.private_mode,
                    changes.archived,
                    _key(binding),
                    conversation_id,
                    changes.expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise CoreError(409, "revision_conflict", "Conversation changed or was deleted")
            if changes.private_mode is True and before is not None:
                self._activate_private(db, binding, conversation_id, before)
        return self.read(binding, conversation_id)

    def _activate_private(
        self,
        db: Connection[tuple[Any, ...]],
        binding: Binding,
        conversation_id: str,
        before: tuple[Any, ...],
    ) -> None:
        if not before[0]:
            db.execute(
                "UPDATE conversations SET private_mode=true,memory_epoch=memory_epoch+1 "
                "WHERE binding=%s AND id=%s",
                (_key(binding), conversation_id),
            )
            revoke(db, _key(binding), conversation_id, before[1] + 1, "private")

    def confirm(
        self, binding: Binding, conversation_id: str, answer: MemoryConfirmation
    ) -> Snapshot:
        with self._connection(binding) as db:
            before = db.execute(
                "SELECT private_mode,memory_epoch FROM conversations WHERE binding=%s AND id=%s",
                (_key(binding), conversation_id),
            ).fetchone()
            result = db.execute(
                "UPDATE conversations SET revision=revision+1 "
                "WHERE binding=%s AND id=%s AND revision=%s",
                (_key(binding), conversation_id, answer.expected_revision),
            )
            if result.rowcount != 1:
                raise CoreError(409, "revision_conflict", "Conversation changed or was deleted")
            turn = db.execute(
                "SELECT memory_confirmation FROM turns "
                "WHERE binding=%s AND conversation=%s AND revision=%s",
                (_key(binding), conversation_id, answer.turn_revision),
            ).fetchone()
            if turn is None:
                raise CoreError(409, "confirmation_conflict", "Confirmation is not pending")
            state = resolve(decode(turn[0]), answer.message_index, answer.accept_private_mode)
            db.execute(
                "UPDATE turns SET memory_confirmation=%s "
                "WHERE binding=%s AND conversation=%s AND revision=%s",
                (json.dumps(state), _key(binding), conversation_id, answer.turn_revision),
            )
            if answer.accept_private_mode and before is not None:
                self._activate_private(db, binding, conversation_id, before)
        return self.read(binding, conversation_id)

    def source_eligible(self, binding: Binding, source: SourceReference) -> bool:
        """Check current source existence/mode and immutable per-turn exclusions.

        This is necessary, not sufficient, for stage-3 memory admission or retrieval.
        A memory consumer must also check privacy, source provenance and its own policy.
        """
        with self._connection(binding) as db:
            row = db.execute(
                "SELECT c.private_mode,t.private_mode,t.memory_excluded,t.messages,"
                "t.memory_confirmation "
                "FROM conversations c JOIN turns t ON c.binding=t.binding AND c.id=t.conversation "
                "WHERE c.binding=%s AND c.id=%s AND t.revision=%s",
                (_key(binding), source.conversation_id, source.turn_revision),
            ).fetchone()
            return bool(
                row is not None
                and not row[0]
                and not row[1]
                and type(source.message_index) is int
                and 0 <= source.message_index < len(json.loads(row[3]))
                and source.message_index not in json.loads(row[2])
                and not blocked(decode(row[4]), source.message_index)
            )

    def deletions(self, binding: Binding) -> tuple[SourceDeletion, ...]:
        """Read durable source deletions for a trusted stage-3 consumer, without content."""
        with self._connection(binding) as db:
            return tuple(
                SourceDeletion(*row)
                for row in db.execute(
                    "SELECT event,conversation,through_revision FROM source_deletions "
                    "WHERE binding=%s ORDER BY seq",
                    (_key(binding),),
                )
            )

    def acknowledge_deletion(self, binding: Binding, event_id: str) -> None:
        """Acknowledge only after a consumer has deleted dependent memory/index data."""
        with self._connection(binding) as db:
            db.execute(
                "DELETE FROM source_deletions WHERE binding=%s AND event=%s",
                (_key(binding), event_id),
            )
