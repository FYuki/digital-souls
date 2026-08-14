from __future__ import annotations

import fcntl
import grp
import os
import subprocess
from pathlib import Path
from typing import TextIO

import pytest

from tests.dogfood_infrastructure_test_support import (
    DOGFOOD_SCRIPTS_DIR,
    TEST_REVISION,
    command_as_service_user,
    command_with_root_owned_revision,
    command_with_root_owned_revision_as_service_user,
    write_dogfood_env,
    write_dogfood_revision,
)


def _open_fixed_env_test_lock(lock_path: Path) -> TextIO:
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    return os.fdopen(descriptor, "r+", encoding="utf-8")


def _run_loader(
    env_path: Path | str,
    sentinel_path: Path,
    *,
    path_environment: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "DOGFOOD_ENV_FILE": str(env_path)}
    if path_environment is not None:
        environment["PATH"] = path_environment
    revision_path = Path(env_path).parent / "config" / "dogfood.revision"
    return subprocess.run(
        command_with_root_owned_revision(
            revision_path,
            [
            "bash",
            "-c",
            'source "$1"; dogfood_load_environment && touch "$2"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
            str(sentinel_path),
            ],
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _replace_setting(env_path: Path, key: str, value: str) -> None:
    source = env_path.read_text(encoding="utf-8")
    lines = source.splitlines()
    updated = [
        f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines
    ]
    env_path.write_text("\n".join(updated), encoding="utf-8")


def test_should_continue_after_loading_valid_dogfood_environment(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    assert env_path.stat().st_mode & 0o777 == 0o600
    sentinel_path = tmp_path / "valid.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode == 0, result.stderr
    assert sentinel_path.is_file()


def test_should_export_revision_loaded_from_the_separate_revision_file(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)

    result = subprocess.run(
        command_with_root_owned_revision(
            tmp_path / "config" / "dogfood.revision",
            [
            "bash",
            "-c",
            'set -e; source "$1"; dogfood_load_environment; '
            'printf "%s" "$DOGFOOD_REPOSITORY_REVISION"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
            ],
        ),
        env={**os.environ, "DOGFOOD_ENV_FILE": str(env_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == TEST_REVISION


def test_should_accept_root_owned_revision_when_loader_runs_as_service_user(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    loader_uid_path = tmp_path / "service-user.uid"
    sentinel_path = tmp_path / "service-user.sentinel"

    result = subprocess.run(
        command_with_root_owned_revision_as_service_user(
            tmp_path / "config" / "dogfood.revision",
            [
                "bash",
                "-c",
                "loader_uid=$(/usr/bin/awk '/^Uid:/{print $2}' /proc/$$/status); "
                'printf "%s\\n" "$loader_uid" > "$2"; '
                '[ "$loader_uid" -ne 0 ]; source "$1"; '
                'dogfood_load_environment && touch "$3"',
                "bash",
                str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
                str(loader_uid_path),
                str(sentinel_path),
            ],
        ),
        env={**os.environ, "DOGFOOD_ENV_FILE": str(env_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert int(loader_uid_path.read_text(encoding="utf-8")) != 0
    assert sentinel_path.is_file()


def test_should_reject_revision_owned_by_service_user(tmp_path: Path) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    sentinel_path = tmp_path / "service-owned-revision.sentinel"

    result = subprocess.run(
        command_as_service_user(
            [
                "bash",
                "-c",
                'source "$1"; dogfood_load_environment && touch "$2"',
                "bash",
                str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
                str(sentinel_path),
            ]
        ),
        env={**os.environ, "DOGFOOD_ENV_FILE": str(env_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not sentinel_path.exists()


@pytest.mark.parametrize(
    "revision",
    ("", "abc", f"{TEST_REVISION}\n{TEST_REVISION}", TEST_REVISION.upper()),
    ids=("empty", "short", "multiple-lines", "uppercase"),
)
def test_should_reject_revision_files_that_do_not_contain_one_complete_sha(
    tmp_path: Path,
    revision: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    write_dogfood_revision(tmp_path, revision)
    sentinel_path = tmp_path / "invalid-revision.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


def test_should_reject_the_removed_revision_environment_key(tmp_path: Path) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    env_path.write_text(
        f"{env_path.read_text(encoding='utf-8')}\n"
        f"DOGFOOD_REPOSITORY_REVISION={TEST_REVISION}\n",
        encoding="utf-8",
    )
    sentinel_path = tmp_path / "legacy-env.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


@pytest.mark.parametrize("revision_state", ("missing", "symlink"))
def test_should_reject_an_untrusted_revision_file(
    tmp_path: Path,
    revision_state: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    revision_path = tmp_path / "config" / "dogfood.revision"
    revision_path.unlink()
    if revision_state == "symlink":
        target = tmp_path / "revision-target"
        target.write_text(f"{TEST_REVISION}\n", encoding="utf-8")
        revision_path.symlink_to(target)
    sentinel_path = tmp_path / "untrusted-revision.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


def test_should_reject_a_revision_file_with_noncanonical_permissions(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    revision_path = tmp_path / "config" / "dogfood.revision"
    revision_path.chmod(0o600)
    sentinel_path = tmp_path / "revision-mode.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


def test_should_reject_a_revision_file_owned_by_a_different_group(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    different_group = next(
        (entry.gr_name for entry in grp.getgrall() if entry.gr_gid != os.getgid()),
        None,
    )
    if different_group is None:
        pytest.skip("実行GIDと異なる既存groupがありません")
    _replace_setting(env_path, "DOGFOOD_SERVICE_GROUP", different_group)
    sentinel_path = tmp_path / "revision-group.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


def test_secret_env_01_exports_normalized_temporary_environment_path(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    alias_parent = tmp_path / "alias"
    alias_parent.mkdir()
    aliased_path = alias_parent / ".." / env_path.name

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; dogfood_load_environment; printf "%s" "$DOGFOOD_RESOLVED_ENV_FILE"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
        ],
        env={**os.environ, "DOGFOOD_ENV_FILE": str(aliased_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(env_path.resolve())


@pytest.mark.parametrize("mode", (0o640, 0o604, 0o666), ids=("group", "other", "both"))
def test_secret_env_01_rejects_exposed_temporary_env_before_reading_it(
    tmp_path: Path,
    mode: int,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    secret = "ab" * 32
    assert secret in env_path.read_text(encoding="utf-8")
    env_path.chmod(mode)
    sentinel_path = tmp_path / "permission-rejected.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_secret_env_01_rejects_symlink_before_reading_secrets(tmp_path: Path) -> None:
    target, _ = write_dogfood_env(tmp_path)
    secret = "ab" * 32
    link = tmp_path / "linked-dogfood.env"
    link.symlink_to(target)
    sentinel_path = tmp_path / "symlink-rejected.sentinel"

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; DOGFOOD_DEFAULT_ENV_FILE=$2; '
            'unset DOGFOOD_ENV_FILE; dogfood_load_environment && touch "$3"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
            str(link),
            str(sentinel_path),
        ],
        env={**os.environ, "DOGFOOD_ENV_FILE": str(link)},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not sentinel_path.exists()
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_secret_env_01_cannot_bypass_mode_validation_with_fake_stat(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    env_path.chmod(0o666)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_stat = fake_bin / "stat"
    fake_stat_sentinel = tmp_path / "fake-stat-called.sentinel"
    fake_stat.write_text(
        "#!/usr/bin/env bash\n"
        f'touch "{fake_stat_sentinel}"\n'
        "printf 'regular file:0:1\\n'\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)
    validation_sentinel = tmp_path / "validation.sentinel"
    export_sentinel = tmp_path / "export.sentinel"
    following_sentinel = tmp_path / "following.sentinel"
    environment = {
        **os.environ,
        "DOGFOOD_ENV_FILE": str(env_path),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; '
            "unset DS_ENVIRONMENT_ID DOGFOOD_RESOLVED_ENV_FILE; "
            'dogfood_validate_environment() { touch "$2"; }; '
            "dogfood_load_environment; status=$?; "
            '[ -z "${DS_ENVIRONMENT_ID-}${DOGFOOD_RESOLVED_ENV_FILE-}" ] '
            '|| touch "$3"; '
            '[ "$status" -ne 0 ] || touch "$4"; '
            'exit "$status"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
            str(validation_sentinel),
            str(export_sentinel),
            str(following_sentinel),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not fake_stat_sentinel.exists()
    assert not validation_sentinel.exists()
    assert not export_sentinel.exists()
    assert not following_sentinel.exists()


def test_secret_env_01_does_not_open_symlink_swapped_after_file_check(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    original_path = tmp_path / "original-dogfood.env"
    fifo_path = tmp_path / "swapped-dogfood.env"
    opened_sentinel = tmp_path / "opened.sentinel"
    validation_sentinel = tmp_path / "validation.sentinel"
    export_sentinel = tmp_path / "export.sentinel"
    following_sentinel = tmp_path / "following.sentinel"
    swap_sentinel = tmp_path / "swapped.sentinel"

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; '
            'mkfifo "$3"; '
            '( exec 3>"$3"; touch "$4"; exec 3>&- ) & writer=$!; '
            'dogfood_validate_environment() { touch "$5"; }; '
            "swap_original=$2; swap_fifo=$3; swap_marker=$8; "
            "dogfood_swap_after_file_check() { "
            "  local command=$1; "
            '  if [ "${swap_armed-}" = 1 ]; then '
            "    trap - DEBUG; "
            '    mv -- "$DOGFOOD_ENV_FILE" "$swap_original"; '
            '    ln -s -- "$swap_fifo" "$DOGFOOD_ENV_FILE"; '
            '    touch "$swap_marker"; '
            "    return; "
            "  fi; "
            '  if [ "$command" = \'[ ! -f "$env_file" ]\' ]; then '
            "    swap_armed=1; "
            "  fi; "
            "}; "
            "set -T; trap 'dogfood_swap_after_file_check \"$BASH_COMMAND\"' DEBUG; "
            "dogfood_load_environment; status=$?; "
            'kill "$writer" 2>/dev/null || true; wait "$writer" 2>/dev/null || true; '
            '[ "${DS_ENVIRONMENT_ID-}" != "dogfood" ] || touch "$6"; '
            '[ "$status" -ne 0 ] || touch "$7"; '
            'exit "$status"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
            str(original_path),
            str(fifo_path),
            str(opened_sentinel),
            str(validation_sentinel),
            str(export_sentinel),
            str(following_sentinel),
            str(swap_sentinel),
        ],
        env={**os.environ, "DOGFOOD_ENV_FILE": str(env_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert swap_sentinel.is_file()
    assert result.returncode != 0
    assert not opened_sentinel.exists()
    assert not validation_sentinel.exists()
    assert not export_sentinel.exists()
    assert not following_sentinel.exists()


@pytest.mark.parametrize(
    "fixed_path_text",
    (
        "/tmp/dogfood.env",
        "/tmp/./dogfood.env",
        "/tmp/x/../dogfood.env",
    ),
    ids=("direct", "current-directory", "parent-directory"),
)
def test_secret_env_01_rejects_the_deprecated_fixed_temporary_path_aliases(
    tmp_path: Path,
    fixed_path_text: str,
) -> None:
    source, _ = write_dogfood_env(tmp_path)
    fixed_path = Path("/tmp/dogfood.env")
    intermediate_path = Path("/tmp/x")
    lock_path = Path("/tmp/digital-souls-dogfood-env-test.lock")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    descriptor_read_sentinel = tmp_path / "descriptor-read.sentinel"
    fake_python.write_text(
        f'#!/usr/bin/env bash\ntouch "{descriptor_read_sentinel}"\nexit 99\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    read_sentinel = tmp_path / "environment-read.sentinel"
    validation_sentinel = tmp_path / "validation.sentinel"
    export_sentinel = tmp_path / "resolved-path-export.sentinel"
    following_sentinel = tmp_path / "fixed-path-rejected.sentinel"
    created_intermediate = False

    with _open_fixed_env_test_lock(lock_path) as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if fixed_path.exists() or fixed_path.is_symlink():
            pytest.skip("/tmp/dogfood.env is already owned by another process")
        if fixed_path_text == "/tmp/x/../dogfood.env":
            if intermediate_path.is_symlink() or (
                intermediate_path.exists() and not intermediate_path.is_dir()
            ):
                pytest.skip("/tmp/x is not available as a regular directory")
            if not intermediate_path.exists():
                intermediate_path.mkdir(mode=0o700)
                created_intermediate = True
        descriptor = os.open(
            fixed_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(source.read_bytes())

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; '
                    "unset DOGFOOD_RESOLVED_ENV_FILE; "
                    'dogfood_read_environment() { touch "$2"; }; '
                    'dogfood_validate_environment() { touch "$3"; }; '
                    "dogfood_load_environment; status=$?; "
                    '[ -z "${DOGFOOD_RESOLVED_ENV_FILE+x}" ] || touch "$4"; '
                    '[ "$status" -ne 0 ] || touch "$5"; '
                    'exit "$status"',
                    "bash",
                    str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
                    str(read_sentinel),
                    str(validation_sentinel),
                    str(export_sentinel),
                    str(following_sentinel),
                ],
                env={
                    **os.environ,
                    "DOGFOOD_ENV_FILE": fixed_path_text,
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
                capture_output=True,
                text=True,
                timeout=10,
            )

            assert result.returncode != 0
            assert not descriptor_read_sentinel.exists()
            assert not read_sentinel.exists()
            assert not validation_sentinel.exists()
            assert not export_sentinel.exists()
            assert not following_sentinel.exists()
        finally:
            fixed_path.unlink(missing_ok=True)
            if created_intermediate:
                intermediate_path.rmdir()


def test_should_reject_symlink_test_lock_without_changing_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "lock-target"
    original_content = "must remain unchanged"
    target.write_text(original_content, encoding="utf-8")
    lock_path = tmp_path / "fixed-env-test.lock"
    lock_path.symlink_to(target)

    with pytest.raises(OSError):
        _open_fixed_env_test_lock(lock_path)

    assert target.read_text(encoding="utf-8") == original_content


@pytest.mark.parametrize(
    "invalid_line",
    (
        "UNKNOWN_DOGFOOD_KEY=value",
        "DS_ENVIRONMENT_ID=dogfood",
    ),
    ids=("unknown-key", "duplicate-key"),
)
def test_should_reject_invalid_env_line_before_following_operation(
    tmp_path: Path,
    invalid_line: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    with env_path.open("a", encoding="utf-8") as env_file:
        env_file.write(f"\n{invalid_line}\n")
    sentinel_path = tmp_path / "invalid-line.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


def test_should_reject_empty_setting_before_following_operation(tmp_path: Path) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    _replace_setting(env_path, "DOGFOOD_VOICEVOX_CONTAINER", "")
    sentinel_path = tmp_path / "empty.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


def test_should_reject_missing_required_setting_before_following_operation(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    source = env_path.read_text(encoding="utf-8")
    env_path.write_text(
        "\n".join(
            line
            for line in source.splitlines()
            if not line.startswith("DOGFOOD_VOICEVOX_CONTAINER=")
        ),
        encoding="utf-8",
    )
    sentinel_path = tmp_path / "missing.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


@pytest.mark.parametrize(
    "key",
    (
        "DOGFOOD_BACKUP_DIR",
        "DOGFOOD_BACKUP_RETENTION_COUNT",
        "DOGFOOD_BACKUP_AUTHENTICATION_KEY",
    ),
)
def test_should_reject_missing_required_backup_setting_before_following_operation(
    tmp_path: Path,
    key: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    source = env_path.read_text(encoding="utf-8")
    env_path.write_text(
        "\n".join(
            line for line in source.splitlines() if not line.startswith(f"{key}=")
        ),
        encoding="utf-8",
    )
    sentinel_path = tmp_path / "missing-backup-setting.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


@pytest.mark.parametrize(
    "key",
    (
        "DOGFOOD_BACKUP_DIR",
        "DOGFOOD_BACKUP_RETENTION_COUNT",
        "DOGFOOD_BACKUP_AUTHENTICATION_KEY",
    ),
)
def test_should_reject_empty_backup_setting_before_following_operation(
    tmp_path: Path,
    key: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    _replace_setting(env_path, key, "")
    sentinel_path = tmp_path / "empty-backup-setting.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


@pytest.mark.parametrize(
    ("key", "invalid_value"),
    (
        ("DOGFOOD_WSL_DISTRO", "Ubuntu dogfood"),
        ("DOGFOOD_SERVICE_USER", "Digital-Souls"),
        ("DOGFOOD_SERVICE_GROUP", "digital souls"),
        ("DOGFOOD_CLONE_DIR", "relative/clone"),
        ("DOGFOOD_REPOSITORY_URL", "http://example.invalid/repository.git"),
        ("DOGFOOD_BACKUP_RETENTION_COUNT", "0"),
        ("DOGFOOD_BACKUP_RETENTION_COUNT", "seven"),
        ("DOGFOOD_BACKUP_AUTHENTICATION_KEY", "0123456789abcdef"),
    ),
    ids=(
        "wsl-distribution",
        "service-user",
        "service-group",
        "absolute-path",
        "repository-url",
        "backup-retention-count",
        "backup-retention-nonnumeric",
        "backup-authentication-key",
    ),
)
def test_should_reject_invalid_setting_format_before_following_operation(
    tmp_path: Path,
    key: str,
    invalid_value: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    _replace_setting(env_path, key, invalid_value)
    sentinel_path = tmp_path / "invalid-format.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


@pytest.mark.parametrize(
    ("left_key", "right_key"),
    (
        ("DOGFOOD_CLONE_DIR", "DOGFOOD_LOG_DIR"),
        ("DOGFOOD_CONFIG_DIR", "DS_DATA_DIR"),
        ("DS_DATA_DIR", "DOGFOOD_STATE_DIR"),
        ("DS_DATA_DIR", "DOGFOOD_BACKUP_DIR"),
    ),
    ids=(
        "first-and-last",
        "adjacent-middle",
        "middle-and-later",
        "data-and-backup",
    ),
)
def test_should_reject_overlapping_paths_before_following_operation(
    tmp_path: Path,
    left_key: str,
    right_key: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    shared_path = tmp_path / "shared"
    _replace_setting(env_path, left_key, str(shared_path))
    _replace_setting(env_path, right_key, str(shared_path / "child"))
    sentinel_path = tmp_path / "overlap.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()
