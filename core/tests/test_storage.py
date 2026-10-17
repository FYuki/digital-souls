"""The trusted factory is explicit and cannot change default API persistence."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from digital_souls_core import storage
from digital_souls_core.postgres_db import PostgresConfig

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"backend": "remote"},
        {"backend": "postgresql"},
        {"backend": "retired", "extra": True},
        {"backend": "retired", "storage_path": ""},
        {"backend": "retired", "postgres": {"database": "synthetic", "user": "synthetic"}},
        {
            "backend": "postgresql",
            "storage_path": "/tmp/synthetic",
            "postgres": {"database": "synthetic", "user": "synthetic"},
        },
    ],
)
def test_ambiguous_config_is_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        storage.StorageConfig.model_validate(values)


def test_postgres_factory_shares_database_and_does_not_extract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = storage.StorageConfig(
        backend="postgresql", postgres=PostgresConfig(database="synthetic", user="synthetic")
    )
    calls: list[object] = []

    def opened(database: object) -> object:
        calls.append(database)
        return object()

    monkeypatch.setattr(storage, "PostgresHistory", opened)
    monkeypatch.setattr(storage, "PostgresMemoryRecords", opened)
    result = storage.open_storage(config)
    assert isinstance(result, storage.StorageStores)
    assert len(calls) == 2 and calls[0] is calls[1]


def test_construct_cannot_bypass_backend_validation() -> None:
    config = storage.StorageConfig.model_construct(backend="postgresql", postgres=None)
    with pytest.raises(ValidationError):
        storage.open_storage(config)


@pytest.mark.parametrize(
    "values",
    [
        {"backend": "retired"},
        {"backend": "retired", "storage_path": "/tmp/synthetic"},
        {
            "backend": "postgresql",
            "postgres": {"database": "synthetic", "user": "synthetic"},
            "storage_path": None,
        },
        {
            "backend": "postgresql",
            "postgres": {"database": "synthetic", "user": "synthetic"},
            "unknown": True,
        },
        {
            "backend": "postgresql",
            "postgres": {"database": "synthetic", "user": "synthetic", "port": "5432"},
        },
        {"backend": b"postgresql", "postgres": {"database": "synthetic", "user": "synthetic"}},
    ],
)
def test_retired_and_non_strict_config_is_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        storage.StorageConfig.model_validate(values)


def test_only_explicit_postgres_fields_and_example_are_supported() -> None:
    schema = storage.StorageConfig.model_json_schema()
    assert set(schema["properties"]) == {"backend", "postgres"}
    assert set(schema["required"]) == {"backend", "postgres"}
    assert schema["properties"]["backend"]["const"] == "postgresql"
    example = Path("examples/storage.postgresql.example.json").read_text(encoding="utf-8")
    config = storage.StorageConfig.model_validate_json(example)
    assert config.backend == "postgresql" and isinstance(config.postgres, PostgresConfig)
