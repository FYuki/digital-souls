from __future__ import annotations

import grp
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path


ROOT_DIR = Path(__file__).parent.parent.parent
DOGFOOD_INFRA_DIR = ROOT_DIR / "infra" / "dogfood"
DOGFOOD_SCRIPTS_DIR = ROOT_DIR / "scripts" / "dogfood"
TEST_REVISION = "0123456789abcdef0123456789abcdef01234567"
TEST_SECRET_SENTINEL = "ab" * 32
TEST_SERVICE_GROUP = grp.getgrgid(os.getgid()).gr_name


def read_valid_deployment_manifest(
    path: Path,
    expected_metadata: dict[str, object],
) -> dict[str, object]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert {key: value for key, value in manifest.items() if key != "deployedAt"} == (
        expected_metadata
    )
    deployed_at = datetime.fromisoformat(manifest["deployedAt"].replace("Z", "+00:00"))
    assert "T" in manifest["deployedAt"]
    assert deployed_at.utcoffset() == timedelta(0)
    assert path.stat().st_mode & 0o777 == 0o640
    return manifest


def write_dogfood_revision(tmp_path: Path, revision: str = TEST_REVISION) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    revision_path = config_dir / "dogfood.revision"
    revision_path.write_text(f"{revision}\n", encoding="utf-8")
    revision_path.chmod(0o640)
    return revision_path


def write_dogfood_env(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_path = tmp_path / "dogfood.env"
    env_path.write_text(
        "\n".join(
            (
                "DS_ENVIRONMENT_ID=dogfood",
                "DOGFOOD_WSL_DISTRO=Ubuntu-dogfood",
                "DOGFOOD_SERVICE_USER=digital-souls",
                f"DOGFOOD_SERVICE_GROUP={TEST_SERVICE_GROUP}",
                "DOGFOOD_REPOSITORY_URL=https://example.invalid/digital-souls.git",
                f"DOGFOOD_CLONE_DIR={tmp_path / 'clone'}",
                f"DOGFOOD_CONFIG_DIR={tmp_path / 'config'}",
                f"DS_DATA_DIR={data_dir}",
                f"DOGFOOD_BACKUP_DIR={tmp_path / 'backups'}",
                "DOGFOOD_BACKUP_RETENTION_COUNT=7",
                f"DOGFOOD_BACKUP_AUTHENTICATION_KEY={TEST_SECRET_SENTINEL}",
                f"DOGFOOD_STATE_DIR={tmp_path / 'state'}",
                f"DOGFOOD_LOG_DIR={tmp_path / 'log'}",
                "DOGFOOD_VOICEVOX_IMAGE=voicevox/voicevox_engine:test",
                "DOGFOOD_VOICEVOX_CONTAINER=digital-souls-voicevox",
            )
        ),
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    write_dogfood_revision(tmp_path)
    return env_path, data_dir


def render_dogfood_assets(
    env_path: Path,
    revision_path: Path,
    output_dir: Path,
) -> None:
    output_dir.mkdir()
    result = subprocess.run(
        command_with_root_owned_revision(
            revision_path,
            [
                "bash",
                "-c",
                'source "$1"; dogfood_load_environment && "$2" "$3" "$4"',
                "bash",
                str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
                str(DOGFOOD_SCRIPTS_DIR / "render-assets.sh"),
                str(DOGFOOD_INFRA_DIR / "templates"),
                str(output_dir),
            ],
        ),
        env={**os.environ, "DOGFOOD_ENV_FILE": str(env_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def render_nondefault_dogfood_assets(tmp_path: Path) -> tuple[dict[str, str], Path]:
    env_path, _ = write_dogfood_env(tmp_path)
    replacements = {
        "DOGFOOD_WSL_DISTRO=Ubuntu-dogfood": "DOGFOOD_WSL_DISTRO=Saab-dogfood",
        "DOGFOOD_SERVICE_USER=digital-souls": "DOGFOOD_SERVICE_USER=soul-service",
    }
    source = env_path.read_text(encoding="utf-8")
    for current, replacement in replacements.items():
        source = source.replace(current, replacement)
    source += f"\nDOGFOOD_SERVICE_HOME_DIR={tmp_path / 'nondefault-service-home'}\n"
    env_path.write_text(source, encoding="utf-8")
    output_dir = tmp_path / "generated"
    revision_path = tmp_path / "config" / "dogfood.revision"
    render_dogfood_assets(env_path, revision_path, output_dir)
    values = {
        line.partition("=")[0]: line.partition("=")[2] for line in source.splitlines()
    }
    return values, output_dir


def write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -eu\n{source}", encoding="utf-8")
    path.chmod(0o755)


def command_with_root_owned_revision(
    revision_path: Path,
    command: list[str],
) -> list[str]:
    return [
        "fakeroot",
        "bash",
        "-c",
        '/usr/bin/chown "0:$1" "$2" 2>/dev/null '
        '|| [ "$(/usr/bin/stat -c %u:%g "$2")" = "0:$1" ]; '
        'shift 2; exec "$@"',
        "bash",
        str(os.getgid()),
        str(revision_path),
        *command,
    ]


def command_as_service_user(command: list[str]) -> list[str]:
    return [
        "setpriv",
        "--reuid",
        str(os.getuid()),
        "--regid",
        str(os.getgid()),
        "--keep-groups",
        *command,
    ]


def command_with_root_owned_revision_as_service_user(
    revision_path: Path,
    command: list[str],
) -> list[str]:
    fakeroot_state_path = revision_path.with_name(".fakeroot-state")
    service_user_command = command_as_service_user(
        ["fakeroot", "-i", str(fakeroot_state_path), *command]
    )
    return [
        "bash",
        "-c",
        'fakeroot -s "$1" bash -c \'/usr/bin/chown "0:$1" "$2" 2>/dev/null '
        '|| [ "$(/usr/bin/stat -c %u:%g "$2")" = "0:$1" ]\' '
        'bash "$2" "$3"; '
        'shift 3; exec "$@"',
        "bash",
        str(fakeroot_state_path),
        str(os.getgid()),
        str(revision_path),
        *service_user_command,
    ]


def _write_bootstrap_clone_assets(clone_dir: Path) -> None:
    setup_backend = clone_dir / "scripts" / "setup-backend.sh"
    setup_backend.parent.mkdir(parents=True, exist_ok=True)
    write_executable(
        setup_backend,
        'printf "backend-setup\\n" >> "$BOOTSTRAP_CALL_LOG"\n',
    )
    renderer = clone_dir / "scripts" / "dogfood" / "render-assets.sh"
    renderer.parent.mkdir(parents=True)
    write_executable(
        renderer,
        'printf "renderer\\n" >> "$BOOTSTRAP_CALL_LOG"\n'
        'touch "$2/digital-souls-ollama.service" '
        '"$2/digital-souls-voicevox.service" '
        '"$2/digital-souls-application.service" "$2/start-dogfood-wsl.ps1"\n',
    )
    target = clone_dir / "infra" / "dogfood" / "systemd"
    target.mkdir(parents=True)
    (target / "digital-souls-inference.target").write_text(
        "[Unit]\nDescription=test\n",
        encoding="utf-8",
    )
    (target / "digital-souls-dogfood.target").write_text(
        "[Unit]\nDescription=test\n",
        encoding="utf-8",
    )
    (clone_dir / "frontend").mkdir()


def prepare_bootstrap_clone(tmp_path: Path) -> Path:
    clone_dir = tmp_path / "clone"
    (clone_dir / ".git").mkdir(parents=True)
    _write_bootstrap_clone_assets(clone_dir)
    return clone_dir


def prepare_initial_bootstrap_clone_assets(tmp_path: Path) -> Path:
    clone_assets_dir = tmp_path / "initial-clone-assets"
    (clone_assets_dir / ".git").mkdir(parents=True)
    _write_bootstrap_clone_assets(clone_assets_dir)
    return clone_assets_dir


def install_bootstrap_command_fakes(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bootstrap-bin"
    bin_dir.mkdir()
    call_log = tmp_path / "bootstrap.calls"
    call_log.touch()
    recorder = 'printf "%s\\t%s\\n" "$(basename "$0")" "$*" >> "$BOOTSTRAP_CALL_LOG"\n'
    write_executable(
        bin_dir / "id",
        recorder
        + 'if [ "${1-}" = "-u" ]; then printf "0\\n"; '
        + 'elif [ "${1-}" = "-gn" ]; then cut -d "|" -f 2 "$BOOTSTRAP_USER_STATE"; '
        + 'elif [ "${1-}" = "-nG" ] && [ "${BOOTSTRAP_DOCKER_MEMBER-}" = "1" ]; '
        + 'then printf "digital-souls docker\\n"; fi\n',
    )
    write_executable(
        bin_dir / "getent",
        recorder
        + 'if [ "${1-}" = "group" ] && [ "${2-}" = "docker" ]; then exit 0; fi\n'
        + 'if [ "${1-}" = "group" ] '
        + '&& [ "${2-}" = "$DOGFOOD_SERVICE_GROUP" ]; then\n'
        + '  [ "${BOOTSTRAP_SERVICE_GROUP_MISSING-}" != "1" ] '
        + '|| [ -f "$BOOTSTRAP_GROUP_CREATED" ]\n'
        + 'elif [ "${1-}" = "passwd" ] && [ "${2-}" = "$DOGFOOD_SERVICE_USER" ]; then\n'
        + '  IFS="|" read -r home _group shell < "$BOOTSTRAP_USER_STATE"\n'
        + '  printf "%s:x:999:999::%s:%s\\n" "$DOGFOOD_SERVICE_USER" "$home" "$shell"\n'
        + "fi\n",
    )
    write_executable(
        bin_dir / "python3",
        recorder
        + 'if [ "$#" -eq 2 ] && [ "$1" = "-" ] '
        + '&& [ -n "${BOOTSTRAP_RESOLVED_SQLITE_PATH-}" ]; then\n'
        + '  printf "%s\\n" "$BOOTSTRAP_RESOLVED_SQLITE_PATH"\n'
        + "  exit 0\n"
        + "fi\n"
        + 'exec /usr/bin/python3 "$@"\n',
    )
    for command in ("gpasswd", "chown", "systemctl", "useradd"):
        write_executable(bin_dir / command, recorder)
    write_executable(
        bin_dir / "usermod",
        recorder
        + "home= group= shell=\n"
        + 'while [ "$#" -gt 1 ]; do\n'
        + '  case "$1" in --home) home=$2 ;; --gid) group=$2 ;; --shell) shell=$2 ;; esac\n'
        + "  shift 2\n"
        + "done\n"
        + 'printf "%s|%s|%s\\n" "$home" "$group" "$shell" > "$BOOTSTRAP_USER_STATE"\n',
    )
    write_executable(
        bin_dir / "groupadd",
        recorder + 'touch "$BOOTSTRAP_GROUP_CREATED"\n',
    )
    write_executable(
        bin_dir / "chmod",
        recorder + 'exec /bin/chmod "$@"\n',
    )
    write_executable(
        bin_dir / "install",
        recorder
        + 'if [ "${1-}" = "-d" ]; then\n'
        + "  shift\n"
        + '  while [ "$#" -gt 0 ]; do\n'
        + '    case "$1" in\n'
        + "      -m) mode=$2; shift 2 ;;\n"
        + "      -o|-g) shift 2 ;;\n"
        + "      /var/lib/digital-souls/*) shift ;;\n"
        + '      *) mkdir -p "$1"; /bin/chmod "$mode" "$1"; shift ;;\n'
        + "    esac\n"
        + "  done\n"
        + "fi\n",
    )
    write_executable(
        bin_dir / "sudo",
        recorder
        + 'if [ "${1-}" = "--preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY" ]; then shift; fi\n'
        + 'if [ "${1-}" = "-u" ]; then export BOOTSTRAP_EFFECTIVE_USER=$2; shift 2; fi\n'
        + 'exec "$@"\n',
    )
    write_executable(
        bin_dir / "docker",
        recorder
        + '[ "$*" = "compose version" ]\n'
        + '[ "${BOOTSTRAP_COMPOSE_AVAILABLE-}" = "1" ]\n',
    )
    write_executable(
        bin_dir / "node",
        recorder
        + '[ "${BOOTSTRAP_NODE_VERSION_EXIT_CODE-0}" -eq 0 ] || '
        + 'exit "$BOOTSTRAP_NODE_VERSION_EXIT_CODE"\n'
        + 'printf "%s\\n" "${BOOTSTRAP_NODE_VERSION-v22.0.0}"\n',
    )
    write_executable(
        bin_dir / "npm",
        recorder
        + 'mkdir -p "$DOGFOOD_CLONE_DIR/frontend/node_modules"\n'
        + 'if [ "${BOOTSTRAP_NPM_DIRTY-}" = "1" ]; then '
        + 'touch "$BOOTSTRAP_NPM_DIRTY_MARKER"; fi\n'
        + 'if [ "${BOOTSTRAP_BUILD_DIRTY-}" = "1" ] '
        + '&& [ "$*" = "--prefix $DOGFOOD_CLONE_DIR/frontend run build" ]; then '
        + '  touch "$BOOTSTRAP_BUILD_DIRTY_MARKER"\n'
        + "fi\n",
    )
    write_executable(
        bin_dir / "git",
        recorder
        + 'case "$*" in\n'
        + '  "config --file "*) exec /usr/bin/git "$@" ;;\n'
        + '  *"clone --no-checkout"*)\n'
        + '    [ "${BOOTSTRAP_FAILURE-}" != "clone" ] || exit 1\n'
        + "    clone_target=${@: -1}\n"
        + '    cp -R "$BOOTSTRAP_INITIAL_CLONE_ASSETS/." "$clone_target" ;;\n'
        + '  *"remote get-url origin"*)\n'
        + '    if [ "${BOOTSTRAP_FAILURE-}" = "origin" ]; then printf "https://example.invalid/other.git\\n"; '
        + 'else printf "%s\\n" "$DOGFOOD_REPOSITORY_URL"; fi ;;\n'
        + '  *"rev-parse --is-shallow-repository"*) printf "false\\n" ;;\n'
        + '  *"fetch origin"*) [ "${BOOTSTRAP_FAILURE-}" != "fetch" ] ;;\n'
        + '  *"rev-parse --verify"*)\n'
        + '    if [ "${BOOTSTRAP_FAILURE-}" = "revision" ]; '
        + 'then printf "ffffffffffffffffffffffffffffffffffffffff\\n"; '
        + 'else printf "%s\\n" "$DOGFOOD_REPOSITORY_REVISION"; fi ;;\n'
        + '  *"checkout --detach"*) touch "$BOOTSTRAP_CHECKED_OUT_MARKER" ;;\n'
        + '  *"rev-parse HEAD"*)\n'
        + '    if [ -f "$BOOTSTRAP_CHECKED_OUT_MARKER" ]; then '
        + 'printf "%s\\n" "$DOGFOOD_REPOSITORY_REVISION"; '
        + 'else printf "%s\\n" "${BOOTSTRAP_CURRENT_HEAD-$DOGFOOD_REPOSITORY_REVISION}"; fi ;;\n'
        + '  *"symbolic-ref --quiet HEAD"*) [ "${BOOTSTRAP_FAILURE-}" = "branch" ] ;;\n'
        + '  *"status --porcelain"*)\n'
        + '    if [ "${BOOTSTRAP_FAILURE-}" = "dirty" ]; then printf " M modified\\n"; '
        + 'elif [ -f "$BOOTSTRAP_NPM_DIRTY_MARKER" ]; then printf " M frontend/package-lock.json\\n"; '
        + 'elif [ -f "$BOOTSTRAP_BUILD_DIRTY_MARKER" ]; then printf "?? frontend/build-runtime-artifact\\n"; fi ;;\n'
        + "esac\n",
    )
    return bin_dir, call_log
