"""接続と専有credentialの保存。secretを含むDBは専用の非公開ディレクトリに置く。"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from app.external_mcp.models import Connection, Json, MCPFailure, encode, now

from .connection_models import ConnectionInput, CredentialInput


@dataclass(frozen=True)
class ConnectionRecord:
    connection: Connection = field(repr=False)
    spec: ConnectionInput = field(repr=False)
    revision: int
    secret_ref: str = field(repr=False)
    credential_set: bool
    last_success_at: str | None
    last_attempt_at: str | None
    last_error_code: str | None
    summary: Json = field(repr=False)


class ConnectionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        # 親はこのstore専用。会話DBや既存data rootのpermissionは変更しない。
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            path.chmod(0o600)
        else:
            os.close(descriptor)
        self._private_values: set[str] = set()
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS connections (
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, manifest TEXT NOT NULL,
                    revision INTEGER NOT NULL, secret_ref TEXT NOT NULL UNIQUE,
                    last_success_at TEXT, last_attempt_at TEXT, last_error_code TEXT,
                    summary TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    connection_id TEXT PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS removed_connections (id TEXT PRIMARY KEY);
            """)
            self._private_values.update(
                row[0] for row in db.execute("SELECT value FROM credentials")
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> ConnectionRecord:
        return ConnectionRecord(
            Connection.from_manifest(json.loads(row["manifest"])),
            ConnectionInput.model_validate_json(row["spec"]),
            row["revision"],
            row["secret_ref"],
            bool(row["credential_set"]),
            row["last_success_at"],
            row["last_attempt_at"],
            row["last_error_code"],
            json.loads(row["summary"]),
        )

    def records(self) -> tuple[ConnectionRecord, ...]:
        with self.transaction() as db:
            rows = db.execute("""SELECT c.*, EXISTS(SELECT 1 FROM credentials s
                WHERE s.connection_id=c.id) AS credential_set FROM connections c ORDER BY c.id""").fetchall()
            return tuple(self._record(row) for row in rows)

    def get(self, connection_id: str) -> ConnectionRecord:
        with self.transaction() as db:
            row = db.execute(
                """SELECT c.*, EXISTS(SELECT 1 FROM credentials s
                WHERE s.connection_id=c.id) AS credential_set FROM connections c WHERE c.id=?""",
                (connection_id,),
            ).fetchone()
            if row is None:
                raise MCPFailure("policy", "unknown_connection")
            return self._record(row)

    def create(self, spec: ConnectionInput) -> ConnectionRecord:
        connection_id = "external-" + uuid4().hex
        ref = "MCP_" + uuid4().hex.upper()
        connection = spec.connection(connection_id, ref)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO connections(id,spec,manifest,revision,secret_ref) VALUES (?,?,?,?,?)",
                (
                    connection_id,
                    spec.model_dump_json(),
                    encode(connection.manifest),
                    1,
                    ref,
                ),
            )
        return self.get(connection_id)

    def update(self, connection_id: str, spec: ConnectionInput) -> ConnectionRecord:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM connections WHERE id=?", (connection_id,)
            ).fetchone()
            if row is None:
                raise MCPFailure("policy", "unknown_connection")
            old = Connection.from_manifest(json.loads(row["manifest"]))
            connection = spec.connection(connection_id, row["secret_ref"], old)
            changed = connection != old
            db.execute(
                "UPDATE connections SET spec=?,manifest=?,revision=revision+? WHERE id=?",
                (
                    spec.model_dump_json(),
                    encode(connection.manifest),
                    int(changed),
                    connection_id,
                ),
            )
            if changed:
                self._reset(db, connection_id)
            if connection.manifest["connection"]["auth"]["type"] == "none":
                db.execute(
                    "DELETE FROM credentials WHERE connection_id=?", (connection_id,)
                )
        return self.get(connection_id)

    @staticmethod
    def _reset(db: sqlite3.Connection, connection_id: str) -> None:
        db.execute(
            """UPDATE connections SET last_success_at=NULL,last_attempt_at=NULL,
            last_error_code=NULL,summary='{}' WHERE id=?""",
            (connection_id,),
        )

    def credential(
        self, connection_id: str, payload: CredentialInput
    ) -> ConnectionRecord:
        value = payload.token.get_secret_value()
        with self.transaction() as db:
            row = db.execute(
                "SELECT manifest FROM connections WHERE id=?", (connection_id,)
            ).fetchone()
            if row is None:
                raise MCPFailure("policy", "unknown_connection")
            if json.loads(row["manifest"])["connection"]["auth"]["type"] != "bearer":
                raise MCPFailure("validation", "credential_not_applicable")
            db.execute(
                "INSERT INTO credentials(connection_id,value) VALUES (?,?) "
                "ON CONFLICT(connection_id) DO UPDATE SET value=excluded.value",
                (connection_id, value),
            )
            db.execute(
                "UPDATE connections SET revision=revision+1 WHERE id=?",
                (connection_id,),
            )
            self._reset(db, connection_id)
        # 更新前の値も実行中の応答から漏らさない。process終了まで非公開値として保持する。
        self._private_values.add(value)
        return self.get(connection_id)

    def delete(self, connection_id: str) -> None:
        with self.transaction() as db:
            if (
                db.execute(
                    "DELETE FROM connections WHERE id=?", (connection_id,)
                ).rowcount
                != 1
            ):
                raise MCPFailure("policy", "unknown_connection")
            # credentialsはFK cascadeで同じtransaction内に削除する。
            db.execute(
                "INSERT OR IGNORE INTO removed_connections(id) VALUES (?)",
                (connection_id,),
            )

    def checked(
        self,
        connection_id: str,
        revision: int,
        *,
        error_code: str | None,
        summary: Json | None = None,
    ) -> bool:
        timestamp = now()
        with self.transaction() as db:
            if error_code is None:
                cursor = db.execute(
                    """UPDATE connections SET last_success_at=?,last_attempt_at=?,
                    last_error_code=NULL,summary=? WHERE id=? AND revision=?""",
                    (
                        timestamp,
                        timestamp,
                        encode(summary or {}),
                        connection_id,
                        revision,
                    ),
                )
            else:
                cursor = db.execute(
                    "UPDATE connections SET last_attempt_at=?,last_error_code=? "
                    "WHERE id=? AND revision=?",
                    (timestamp, error_code, connection_id, revision),
                )
            return cursor.rowcount == 1

    def import_connection(
        self, connection: Connection, display_name: str, token: str | None
    ) -> None:
        """既存設定を一度だけ取り込む。削除済み接続を設定ファイルから復活させない。"""
        connection_id = connection.id
        with self.transaction() as db:
            if db.execute(
                "SELECT id FROM connections WHERE id=? UNION SELECT id FROM removed_connections WHERE id=?",
                (connection_id, connection_id),
            ).fetchone():
                return
            spec = ConnectionInput.from_connection(connection, display_name)
            ref = "MCP_" + uuid4().hex.upper()
            managed = spec.connection(connection_id, ref, connection)
            db.execute(
                "INSERT INTO connections(id,spec,manifest,revision,secret_ref) VALUES (?,?,?,?,?)",
                (
                    connection_id,
                    spec.model_dump_json(),
                    encode(managed.manifest),
                    1,
                    ref,
                ),
            )
            if token:
                value = CredentialInput.model_validate(
                    {"token": token}
                ).token.get_secret_value()
                db.execute(
                    "INSERT INTO credentials(connection_id,value) VALUES (?,?)",
                    (connection_id, value),
                )
                self._private_values.add(value)

    async def resolve(self, secret_ref: str) -> str:
        with self.transaction() as db:
            row = db.execute(
                "SELECT s.value FROM credentials s JOIN connections c ON c.id=s.connection_id "
                "WHERE c.secret_ref=?",
                (secret_ref,),
            ).fetchone()
            if row is None:
                raise MCPFailure("auth", "secret_unavailable")
            return str(row[0])

    def private_values(self) -> tuple[str, ...]:
        # endpointとsecret_refも外部本文やLLM入力へ流さない。
        values = set(self._private_values)
        for record in self.records():
            config = record.connection.manifest["connection"]
            values.update((record.secret_ref, config.get("endpoint", "")))
        return tuple(value for value in values if value)
