import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.runtime_paths import RESTORE_INTENT_FILENAME


def _paths(data_root: Path, repository_root: Path, environment_id: str = "test"):
    from app.runtime_paths import resolve_runtime_paths

    return resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": environment_id,
            "DS_DATA_DIR": str(data_root),
        },
        repository_root,
    )


def _initialize(paths, repository_root: Path) -> None:
    from app.runtime_data_root import initialize_runtime_data_root

    initialize_runtime_data_root(paths, repository_root)


def _write_matching_marker(paths) -> None:
    paths.identity_marker_path.write_text(
        json.dumps({"schemaVersion": 1, "environmentId": "test"}),
        encoding="utf-8",
    )


def test_rt_id_01_creates_strict_identity_marker_for_new_root(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(tmp_path / "data", repository_root)

    _initialize(paths, repository_root)

    assert json.loads(paths.identity_marker_path.read_text(encoding="utf-8")) == {
        "schemaVersion": 1,
        "environmentId": "test",
    }


def test_rt_id_01_accepts_an_existing_matching_marker_without_rewriting_it(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(tmp_path / "data", repository_root)
    paths.data_root.mkdir()
    marker_text = '{"schemaVersion": 1, "environmentId": "test"}\n'
    paths.identity_marker_path.write_text(marker_text, encoding="utf-8")

    _initialize(paths, repository_root)

    assert paths.identity_marker_path.read_text(encoding="utf-8") == marker_text


def test_rt_id_01_prepares_leases_for_both_sqlite_databases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import runtime_data_root

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(tmp_path / "data", repository_root)
    leased_paths: list[Path] = []
    monkeypatch.setattr(
        runtime_data_root,
        "ensure_sqlite_lease_file",
        lambda database_path: leased_paths.append(database_path),
    )

    _initialize(paths, repository_root)

    assert leased_paths == [paths.sqlite_path, paths.persona_memory_sqlite_path]


@pytest.mark.parametrize(
    "marker",
    [
        "not-json",
        "[]",
        '{"schemaVersion": 2, "environmentId": "test"}',
        '{"schemaVersion": 1}',
        '{"schemaVersion": 1, "environmentId": "test", "secret": "x"}',
    ],
)
def test_rt_id_01_rejects_malformed_marker_without_changing_data(
    marker: str, tmp_path: Path
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(tmp_path / "data", repository_root)
    paths.data_root.mkdir()
    sentinel = paths.data_root / "conversation-history.db"
    sentinel.write_bytes(b"dogfood-history")
    paths.identity_marker_path.write_text(marker, encoding="utf-8")

    with pytest.raises(ValueError, match="identity marker"):
        _initialize(paths, repository_root)

    assert sentinel.read_bytes() == b"dogfood-history"
    assert paths.identity_marker_path.read_text(encoding="utf-8") == marker


def test_rt_id_01_rejects_missing_marker_for_root_with_persistent_data(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(tmp_path / "data", repository_root)
    paths.data_root.mkdir()
    sentinel = paths.data_root / "conversation-history.db"
    sentinel.write_bytes(b"existing-history")

    with pytest.raises(ValueError, match="identity marker"):
        _initialize(paths, repository_root)

    assert sentinel.read_bytes() == b"existing-history"
    assert not paths.identity_marker_path.exists()


@pytest.mark.parametrize(
    ("configured_id", "marker_id"),
    [("dev", "dogfood"), ("test", "dogfood"), ("dogfood", "dev"), ("dogfood", "test")],
)
def test_rt_id_01_rejects_cross_environment_marker_without_rewriting_it(
    configured_id: str,
    marker_id: str,
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    data_root = tmp_path / "data"
    paths = _paths(data_root, repository_root, configured_id)
    data_root.mkdir()
    marker = {"schemaVersion": 1, "environmentId": marker_id}
    paths.identity_marker_path.write_text(json.dumps(marker), encoding="utf-8")
    sentinel = data_root / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="environment identity"):
        _initialize(paths, repository_root)

    assert json.loads(paths.identity_marker_path.read_text(encoding="utf-8")) == marker
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_rt_id_01_rejects_cross_environment_marker_before_preparing_sqlite_leases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import runtime_data_root

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(tmp_path / "data", repository_root)
    paths.data_root.mkdir()
    paths.identity_marker_path.write_text(
        json.dumps({"schemaVersion": 1, "environmentId": "dogfood"}),
        encoding="utf-8",
    )
    leased_paths: list[Path] = []
    monkeypatch.setattr(
        runtime_data_root,
        "ensure_sqlite_lease_file",
        lambda database_path: leased_paths.append(database_path),
    )

    with pytest.raises(ValueError, match="environment identity"):
        _initialize(paths, repository_root)

    assert leased_paths == []


def test_rt_safe_01_rejects_symlink_data_root_without_touching_target(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    link = tmp_path / "data-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        paths = _paths(link, repository_root)
        _initialize(paths, repository_root)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (target / ".environment-identity.json").exists()


def test_rt_safe_01_rejects_regular_file_as_data_root(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    data_file = tmp_path / "data"
    data_file.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="directory"):
        paths = _paths(data_file, repository_root)
        _initialize(paths, repository_root)

    assert data_file.read_text(encoding="utf-8") == "keep"


def test_rt_id_01_allows_gitkeep_only_scaffolding_to_receive_a_marker(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(tmp_path / "data", repository_root)
    paths.data_root.mkdir()
    (paths.data_root / ".gitkeep").write_text("", encoding="utf-8")

    _initialize(paths, repository_root)

    assert json.loads(paths.identity_marker_path.read_text(encoding="utf-8")) == {
        "schemaVersion": 1,
        "environmentId": "test",
    }


def test_rt_id_01_concurrent_initialization_keeps_one_valid_marker(
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(tmp_path / "data", repository_root)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _index: _initialize(paths, repository_root), range(2))
        )

    assert results == [None, None]
    assert json.loads(paths.identity_marker_path.read_text(encoding="utf-8")) == {
        "schemaVersion": 1,
        "environmentId": "test",
    }


def test_rt_safe_01_rejects_unwritable_existing_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import runtime_data_root

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    paths = _paths(data_root, repository_root)
    real_access = os.access
    monkeypatch.setattr(
        runtime_data_root.os,
        "access",
        lambda path, mode: (
            False if Path(path) == data_root else real_access(path, mode)
        ),
    )

    with pytest.raises(ValueError, match="writable"):
        _initialize(paths, repository_root)

    assert not paths.identity_marker_path.exists()


@pytest.mark.parametrize("operation", ["initialize", "validate"])
def test_should_reject_invalid_environment_id_before_creating_runtime_files(
    operation: str,
    tmp_path: Path,
) -> None:
    from app.runtime_data_root import (
        initialize_runtime_data_root,
        validate_existing_runtime_data_root,
    )

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    data_root = tmp_path / "data"
    paths = replace(_paths(data_root, repository_root), environment_id="production")
    selected_operation = (
        initialize_runtime_data_root
        if operation == "initialize"
        else validate_existing_runtime_data_root
    )

    with pytest.raises(ValueError):
        selected_operation(paths, repository_root)

    assert not data_root.exists()


@pytest.mark.parametrize(
    ("relative_path", "target_is_directory"),
    [
        ("conversation-history.db", False),
        ("persona-memory.db", False),
        ("chroma", True),
        ("runtime", True),
        ("cache", True),
        ("cache/huggingface/hub", True),
        (".environment-identity.json", False),
        (".environment-identity.lock", False),
        (RESTORE_INTENT_FILENAME, False),
    ],
)
@pytest.mark.parametrize("operation", ["initialize", "validate"])
def test_rt_safe_02_rejects_derived_path_symlink_before_marker_access(
    relative_path: str,
    target_is_directory: bool,
    operation: str,
    tmp_path: Path,
) -> None:
    from app.runtime_data_root import (
        initialize_runtime_data_root,
        validate_existing_runtime_data_root,
    )

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(tmp_path / "data", repository_root)
    paths.data_root.mkdir()
    _write_matching_marker(paths)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    link = paths.data_root / relative_path
    link.parent.mkdir(parents=True, exist_ok=True)
    target = external / ("target-directory" if target_is_directory else "target-file")
    if target_is_directory:
        target.mkdir()
    else:
        target.write_text(
            '{"schemaVersion": 1, "environmentId": "test"}'
            if relative_path == ".environment-identity.json"
            else "external-data",
            encoding="utf-8",
        )
        if link.exists():
            link.unlink()
    link.symlink_to(target, target_is_directory=target_is_directory)

    selected_operation = (
        initialize_runtime_data_root
        if operation == "initialize"
        else validate_existing_runtime_data_root
    )
    with pytest.raises(ValueError):
        selected_operation(paths, repository_root)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert target.exists()


@pytest.mark.parametrize(
    "field_name",
    [
        "sqlite_path",
        "persona_memory_sqlite_path",
        "chroma_path",
        "runtime_report_dir",
        "cache_path",
        "whisper_cache_path",
        "identity_marker_path",
        "restore_intent_path",
    ],
)
@pytest.mark.parametrize("operation", ["initialize", "validate"])
def test_rt_safe_02_rejects_derived_path_outside_canonical_data_root(
    field_name: str,
    operation: str,
    tmp_path: Path,
) -> None:
    from app.runtime_data_root import (
        initialize_runtime_data_root,
        validate_existing_runtime_data_root,
    )

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = _paths(tmp_path / "data", repository_root)
    paths.data_root.mkdir()
    _write_matching_marker(paths)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    outside_path = external / field_name
    if field_name == "identity_marker_path":
        outside_path.write_text(
            '{"schemaVersion": 1, "environmentId": "test"}',
            encoding="utf-8",
        )
    invalid_paths = replace(paths, **{field_name: outside_path})
    selected_operation = (
        initialize_runtime_data_root
        if operation == "initialize"
        else validate_existing_runtime_data_root
    )

    with pytest.raises(ValueError):
        selected_operation(invalid_paths, repository_root)

    assert sentinel.read_text(encoding="utf-8") == "keep"
