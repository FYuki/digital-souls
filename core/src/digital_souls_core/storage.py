"""Explicit trusted storage selection; importing this module opens no database."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .history import HistoryStore
from .memory_record_store import MemoryRecordStore
from .postgres_db import PostgresConfig, PostgresDatabase
from .postgres_history import PostgresHistory
from .postgres_memory_records import PostgresMemoryRecords


class StorageConfig(BaseModel):
    """Deployment-owned values, separate from history and memory authorization."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    backend: Literal["postgresql"]
    postgres: PostgresConfig = Field(repr=False)


@dataclass(frozen=True)
class StorageStores:
    history: HistoryStore
    records: MemoryRecordStore


def open_storage(config: StorageConfig) -> StorageStores:
    """Open stores only on explicit invocation; never import or extract history."""
    config = StorageConfig.model_validate(config.model_dump())
    database = PostgresDatabase(config.postgres)
    return StorageStores(PostgresHistory(database), PostgresMemoryRecords(database))
