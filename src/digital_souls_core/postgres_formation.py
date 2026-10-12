"""Durable coalescing reservations and bounded, restart-from-zero drain."""

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from psycopg import Connection

from .application import CoreError
from .character import AccessScope
from .formation import (
    DrainResult,
    FormationConfig,
    FormationInput,
    FormationProcessor,
    FormationSource,
    job_limit,
)
from .history import Binding, Clock, HistoryStore, as_utc, current_utc
from .postgres_db import _lock_key, key
from .postgres_history import PostgresHistory
from .privacy import PrivacyPolicy


def reserve(
    db: Connection[tuple[Any, ...]], binding: Binding, conversation: str, revision: int
) -> None:
    """Called only in the history commit transaction, after checking permission."""
    db.execute(
        "INSERT INTO formation_jobs(binding,conversation,target_revision,state,attempts,"
        "available_at,last_code,created_at,updated_at) "
        "VALUES (%s,%s,%s,'pending',0,clock_timestamp(),'reserved',clock_timestamp(),"
        "clock_timestamp()) ON CONFLICT(binding,conversation) DO UPDATE SET "
        "target_revision=EXCLUDED.target_revision, "
        "state=CASE WHEN formation_jobs.state='leased' THEN 'leased' ELSE 'pending' END, "
        "attempts=CASE WHEN formation_jobs.state='leased' THEN formation_jobs.attempts ELSE 0 END, "
        "available_at=EXCLUDED.available_at, updated_at=EXCLUDED.updated_at",
        (key(binding), conversation, revision),
    )


@dataclass(frozen=True)
class _Lease:
    binding: Binding
    conversation: str
    revision: int
    token: str
    attempts: int


class PostgresFormation:
    def __init__(
        self,
        history: HistoryStore,
        policy: PrivacyPolicy,
        processor: FormationProcessor | None = None,
        *,
        config: FormationConfig | None = None,
        clock: Clock = current_utc,
    ) -> None:
        if not isinstance(history, PostgresHistory) or history.formation_policy is not policy:
            raise ValueError("formation requires a shared enabled PostgreSQL history policy")
        self.history = history
        self.policy = policy
        self.processor = processor
        self.config = FormationConfig.model_validate((config or FormationConfig()).model_dump())
        self.database = history.database
        self._clock = clock

    def _claim(self, db: Connection[tuple[Any, ...]], seen: list[int]) -> _Lease | None:
        now = as_utc(self._clock())
        # Each call considers a finite candidate window. A live processor retains its
        # session lock even past lease expiry, so expired does not mean duplicate-safe.
        rows = db.execute(
            "SELECT seq,binding,conversation,target_revision,attempts,leased_revision "
            "FROM formation_jobs "
            "WHERE ((state='pending' AND available_at<=%s) OR "
            "(state='leased' AND lease_until<=%s)) AND NOT (seq=ANY(%s)) "
            "ORDER BY available_at,seq LIMIT %s FOR UPDATE SKIP LOCKED",
            (now, now, seen, self.config.max_jobs),
        ).fetchall()
        for seq, binding_key, conversation, revision, attempts, leased_revision in rows:
            lock = _lock_key(
                "formation",
                self.database.config.schema_name,
                json.dumps([binding_key, conversation]),
            )
            acquired = db.execute("SELECT pg_try_advisory_lock(%s)", (lock,)).fetchone()
            if acquired != (True,):
                continue
            seen.append(seq)
            subject, client, audience, character_id = json.loads(binding_key)
            binding = Binding(
                AccessScope(subject=subject, client=client, audience=audience), character_id
            )
            if leased_revision is not None and revision > leased_revision:
                attempts = 0
            token = str(uuid4())
            # Interrupted attempts are bounded as well. No partial progress is loaded.
            db.execute(
                "UPDATE formation_jobs SET state='leased',leased_revision=target_revision,"
                "lease_token=%s,lease_until=%s,attempts=%s,updated_at=%s "
                "WHERE seq=%s",
                (
                    token,
                    now + timedelta(seconds=self.config.lease_seconds),
                    attempts + 1 if attempts < self.config.max_attempts else attempts,
                    now,
                    seq,
                ),
            )
            db.commit()
            return _Lease(binding, conversation, revision, token, attempts + 1)
        db.commit()
        return None

    def _input(self, lease: _Lease) -> FormationInput | None:
        if not self.policy.permits(lease.binding, "memory"):
            return None
        try:
            snapshot = self.history.read(lease.binding, lease.conversation)
        except CoreError as error:
            if error.code == "conversation_not_found":
                return None
            raise
        if snapshot.private_mode:
            return None
        with self.database.transaction(lease.binding) as db:
            row = db.execute(
                "SELECT revision,memory_epoch,private_mode FROM conversations "
                "WHERE binding=%s AND id=%s",
                (key(lease.binding), lease.conversation),
            ).fetchone()
            if row is None or row[2]:
                return None
            if row[0] != snapshot.revision:
                raise CoreError(409, "formation_source_changed", "Formation source changed")
            sources = tuple(
                FormationSource(source, message.model_copy(deep=True))
                for source, message in zip(snapshot.memory_sources, snapshot.messages, strict=True)
                if source.eligible and source.reference.turn_revision <= lease.revision
            )
            if not sources or not self.policy.permits(lease.binding, "memory"):
                return None
            return FormationInput(
                lease.binding, lease.conversation, lease.revision, row[1], sources
            )

    def _finish(self, db: Connection[tuple[Any, ...]], lease: _Lease, code: str) -> None:
        now = as_utc(self._clock())
        failed = code in {"processor_failed", "processor_timeout", "retry_exhausted"}
        state = (
            "failed"
            if failed and lease.attempts >= self.config.max_attempts
            else ("pending" if failed else "done")
        )
        db.execute(
            "UPDATE formation_jobs SET state=CASE WHEN target_revision>leased_revision "
            "THEN 'pending' ELSE %s END, "
            "attempts=CASE WHEN target_revision>leased_revision THEN 0 ELSE attempts END, "
            "available_at=CASE WHEN target_revision>leased_revision THEN %s ELSE %s END, "
            "lease_token=NULL,lease_until=NULL,leased_revision=NULL,last_code=%s,updated_at=%s "
            "WHERE binding=%s AND conversation=%s AND state='leased' AND lease_token=%s",
            (
                state,
                now,
                now + timedelta(seconds=self.config.retry_backoff_seconds) if failed else now,
                code,
                now,
                key(lease.binding),
                lease.conversation,
                lease.token,
            ),
        )
        db.commit()

    async def drain(self, *, max_jobs: int | None = None) -> DrainResult:
        limit = job_limit(self.config, max_jobs)
        processor = self.processor
        if processor is None:
            raise CoreError(
                503, "formation_processor_unavailable", "Formation processor unavailable"
            )
        succeeded = failed = excluded = 0
        seen: list[int] = []
        for _ in range(limit):
            # Closing the dedicated connection releases session locks, including on
            # cancellation. The committed lease survives interruption until expiry.
            with self.database.formation_connection() as db:
                lease = self._claim(db, seen)
                if lease is None:
                    break
                code = "succeeded"
                try:
                    if lease.attempts > self.config.max_attempts:
                        code = "retry_exhausted"
                    else:
                        job = self._input(lease)
                        if job is None:
                            code = "source_excluded"
                        else:
                            async with asyncio.timeout(self.config.processor_timeout_seconds):
                                await processor.process(job)
                except TimeoutError:
                    code = "processor_timeout"
                except Exception:
                    code = "processor_failed"
                self._finish(db, lease, code)
                if code in {"processor_failed", "processor_timeout", "retry_exhausted"}:
                    failed += 1
                elif code == "source_excluded":
                    excluded += 1
                else:
                    succeeded += 1
        return DrainResult(succeeded, failed, excluded)
