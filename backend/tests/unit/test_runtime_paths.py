from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


def _resolve(environment: dict[str, str], repository_root: Path):
    from app.runtime_paths import resolve_runtime_paths

    return resolve_runtime_paths(environment, repository_root)


@pytest.mark.parametrize("environment_id", ["dev", "test", "dogfood"])
def test_rt_env_01_preserves_each_supported_environment_identity(
    environment_id: str, tmp_path: Path
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    data_root = tmp_path / f"{environment_id}-data"

    paths = _resolve(
        {
            "DS_ENVIRONMENT_ID": environment_id,
            "DS_DATA_DIR": str(data_root),
        },
        repository_root,
    )

    assert paths.environment_id == environment_id


def test_rt_env_01_uses_dev_only_when_environment_identity_is_unset(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    paths = _resolve({}, repository_root)

    assert paths.environment_id == "dev"
    assert paths.data_root == repository_root / "backend" / "app" / "data"


@pytest.mark.parametrize("environment_id", ["", "production", " dev", "DEV"])
def test_rt_env_01_rejects_empty_unknown_or_noncanonical_identity(
    environment_id: str, tmp_path: Path
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    with pytest.raises(ValueError, match="DS_ENVIRONMENT_ID"):
        _resolve(
            {
                "DS_ENVIRONMENT_ID": environment_id,
                "DS_DATA_DIR": str(tmp_path / "data"),
            },
            repository_root,
        )


def test_rt_path_01_resolves_every_persistent_path_from_one_data_root(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    data_root = tmp_path / "runtime-data"

    paths = _resolve(
        {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(data_root)},
        repository_root,
    )

    assert paths.data_root == data_root
    assert paths.sqlite_path == data_root / "conversation-history.db"
    assert paths.chroma_path == data_root / "chroma"
    assert paths.runtime_report_dir == data_root / "runtime"
    assert paths.cache_path == data_root / "cache"
    assert paths.whisper_cache_path == data_root / "cache" / "huggingface" / "hub"
    assert paths.identity_marker_path == data_root / ".environment-identity.json"


def test_rt_path_01_returns_an_immutable_resolved_snapshot(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _resolve(
        {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(tmp_path / "data")},
        repository_root,
    )

    with pytest.raises(FrozenInstanceError):
        paths.environment_id = "dogfood"


def test_rt_path_01_keeps_existing_dev_sqlite_and_chroma_defaults(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    paths = _resolve({}, repository_root)

    assert paths.sqlite_path == (
        repository_root / "backend" / "app" / "data" / "conversation-history.db"
    )
    assert paths.chroma_path == repository_root / "backend" / "app" / "data" / "chroma"


def test_rt_safe_01_rejects_relative_data_root(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    with pytest.raises(ValueError, match="DS_DATA_DIR.*absolute"):
        _resolve(
            {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": "relative/data"},
            repository_root,
        )


@pytest.mark.parametrize("dangerous_root", [Path("/"), Path.home()])
def test_rt_safe_01_rejects_dangerous_broad_data_roots(
    dangerous_root: Path, tmp_path: Path
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    with pytest.raises(ValueError, match="DS_DATA_DIR"):
        _resolve(
            {
                "DS_ENVIRONMENT_ID": "dev",
                "DS_DATA_DIR": str(dangerous_root),
            },
            repository_root,
        )


def test_rt_safe_01_rejects_repository_root_itself(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    with pytest.raises(ValueError, match="DS_DATA_DIR"):
        _resolve(
            {"DS_ENVIRONMENT_ID": "dev", "DS_DATA_DIR": str(repository_root)},
            repository_root,
        )


def test_rt_safe_01_rejects_dogfood_data_below_repository(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    with pytest.raises(ValueError, match="dogfood"):
        _resolve(
            {
                "DS_ENVIRONMENT_ID": "dogfood",
                "DS_DATA_DIR": str(repository_root / "runtime-data"),
            },
            repository_root,
        )
