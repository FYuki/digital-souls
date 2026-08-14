from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT_DIR = Path(__file__).parent.parent.parent
DOGFOOD_INFRA_DIR = ROOT_DIR / "infra" / "dogfood"
DOGFOOD_SCRIPTS_DIR = ROOT_DIR / "scripts" / "dogfood"


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
                "DOGFOOD_SERVICE_GROUP=digital-souls",
                "DOGFOOD_REPOSITORY_URL=https://example.invalid/digital-souls.git",
                "DOGFOOD_REPOSITORY_REVISION=0123456789abcdef0123456789abcdef01234567",
                f"DOGFOOD_CLONE_DIR={tmp_path / 'clone'}",
                f"DOGFOOD_CONFIG_DIR={tmp_path / 'config'}",
                f"DS_DATA_DIR={data_dir}",
                f"DOGFOOD_BACKUP_DIR={tmp_path / 'backups'}",
                "DOGFOOD_BACKUP_RETENTION_COUNT=7",
                f"DOGFOOD_BACKUP_AUTHENTICATION_KEY={'ab' * 32}",
                f"DOGFOOD_STATE_DIR={tmp_path / 'state'}",
                f"DOGFOOD_LOG_DIR={tmp_path / 'log'}",
                "DOGFOOD_VOICEVOX_IMAGE=voicevox/voicevox_engine:test",
                "DOGFOOD_VOICEVOX_CONTAINER=digital-souls-voicevox",
            )
        ),
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    return env_path, data_dir


def render_nondefault_dogfood_assets(tmp_path: Path) -> tuple[dict[str, str], Path]:
    env_path, _ = write_dogfood_env(tmp_path)
    replacements = {
        "DOGFOOD_WSL_DISTRO=Ubuntu-dogfood": "DOGFOOD_WSL_DISTRO=Saab-dogfood",
        "DOGFOOD_SERVICE_USER=digital-souls": "DOGFOOD_SERVICE_USER=soul-service",
        "DOGFOOD_SERVICE_GROUP=digital-souls": "DOGFOOD_SERVICE_GROUP=soul-group",
    }
    source = env_path.read_text(encoding="utf-8")
    for current, replacement in replacements.items():
        source = source.replace(current, replacement)
    env_path.write_text(source, encoding="utf-8")
    output_dir = tmp_path / "generated"
    output_dir.mkdir()
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; dogfood_load_environment; "$2" "$3" "$4"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
            str(DOGFOOD_SCRIPTS_DIR / "render-assets.sh"),
            str(DOGFOOD_INFRA_DIR / "templates"),
            str(output_dir),
        ],
        env={**os.environ, "DOGFOOD_ENV_FILE": str(env_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    values = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in source.splitlines()
    }
    return values, output_dir


def write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -eu\n{source}", encoding="utf-8")
    path.chmod(0o755)


def _write_bootstrap_clone_assets(clone_dir: Path) -> None:
    renderer = clone_dir / "scripts" / "dogfood" / "render-assets.sh"
    renderer.parent.mkdir(parents=True)
    write_executable(
        renderer,
        'printf "renderer\\n" >> "$BOOTSTRAP_CALL_LOG"\n'
        'touch "$2/digital-souls-ollama.service" '
        '"$2/digital-souls-voicevox.service" "$2/start-dogfood-wsl.ps1"\n',
    )
    target = clone_dir / "infra" / "dogfood" / "systemd"
    target.mkdir(parents=True)
    (target / "digital-souls-inference.target").write_text(
        "[Unit]\nDescription=test\n",
        encoding="utf-8",
    )


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


def prepare_target_bootstrap_clone_assets(tmp_path: Path) -> Path:
    clone_assets_dir = tmp_path / "target-clone-assets"
    (clone_assets_dir / ".git").mkdir(parents=True)
    setup_backend = clone_assets_dir / "scripts" / "setup-backend.sh"
    setup_backend.parent.mkdir(parents=True)
    write_executable(
        setup_backend,
        '[ "${BOOTSTRAP_FAILURE-}" != "backend-setup" ]\n'
        'printf "backend-setup\\n" >> "$BOOTSTRAP_CALL_LOG"\n'
        'script_dir=$(dirname "$0")\n'
        'python=$(dirname "$script_dir")/backend/.venv/bin/python\n'
        'mkdir -p "$(dirname "$python")"\n'
        'cat > "$python" <<\'PYTHON\'\n'
        '#!/usr/bin/env bash\n'
        'set -eu\n'
        'printf "python\\t%s\\n" "$*" >> "$BOOTSTRAP_CALL_LOG"\n'
        'if [ "$1" = "-c" ]; then exec /usr/bin/python3 "$@"; fi\n'
        'script=$1\n'
        'shift\n'
        'exec bash "$script" "$@"\n'
        'PYTHON\n'
        'chmod 0750 "$python"\n',
    )
    cli = clone_assets_dir / "environments" / "environment_cli.py"
    cli.parent.mkdir(parents=True)
    write_executable(
        cli,
        '[ "$BOOTSTRAP_EFFECTIVE_USER" = "$DOGFOOD_SERVICE_USER" ]\n'
        'repository_root=$(dirname "$(dirname "$0")")\n'
        '[ "$(stat -c %a "$repository_root")" = "2750" ]\n'
        '[ -z "$(find "$repository_root" -type d ! -perm -g=rx -print -quit)" ]\n'
        '[ -z "$(find "$repository_root" -type f ! -perm -g=r -print -quit)" ]\n'
        '[ -d "$DOGFOOD_BACKUP_DIR" ]\n'
        'printf "target-cli\\t%s\\n" "$*" >> "$BOOTSTRAP_CALL_LOG"\n'
        'case "$1" in\n'
        '  backup)\n'
        '    [ "${BOOTSTRAP_FAILURE-}" != "backup" ]\n'
        '    printf \'%s\\n\' "$BOOTSTRAP_BACKUP_OUTPUT" ;;\n'
        '  backup-verify) [ "${BOOTSTRAP_FAILURE-}" != "verify" ] ;;\n'
        'esac\n',
    )
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
        + 'elif [ "${1-}" = "-nG" ] && [ "${BOOTSTRAP_DOCKER_MEMBER-}" = "1" ]; '
        + 'then printf "digital-souls docker\\n"; fi\n',
    )
    write_executable(bin_dir / "getent", recorder)
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
    for command in ("gpasswd", "chown", "systemctl"):
        write_executable(bin_dir / command, recorder)
    write_executable(bin_dir / "chmod", recorder + 'exec /bin/chmod "$@"\n')
    write_executable(
        bin_dir / "install",
        recorder
        + 'if [ "${1-}" = "-d" ]; then\n'
        + "  shift\n"
        + '  while [ "$#" -gt 0 ]; do\n'
        + '    case "$1" in\n'
        + '      -m) mode=$2; shift 2 ;;\n'
        + '      -o|-g) shift 2 ;;\n'
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
        bin_dir / "git",
        recorder
        + 'case "$*" in\n'
        + '  *"clone --no-checkout"*)\n'
        + '    [ "${BOOTSTRAP_FAILURE-}" != "clone" ] || exit 1\n'
        + '    clone_target=${@: -1}\n'
        + '    if [ "$clone_target" = "$DOGFOOD_CLONE_DIR" ]; then\n'
        + '      cp -R "$BOOTSTRAP_INITIAL_CLONE_ASSETS/." "$clone_target"\n'
        + "    else\n"
        + '      cp -R "$BOOTSTRAP_TARGET_CLONE_ASSETS/." "$clone_target"\n'
        + "    fi ;;\n"
        + '  *"remote get-url origin"*)\n'
        + '    if [ "${BOOTSTRAP_FAILURE-}" = "origin" ]; then printf "https://example.invalid/other.git\\n"; '
        + 'else printf "%s\\n" "$DOGFOOD_REPOSITORY_URL"; fi ;;\n'
        + '  *"fetch --depth 1"*) [ "${BOOTSTRAP_FAILURE-}" != "fetch" ] ;;\n'
        + '  *"rev-parse --verify"*)\n'
        + '    if [ "${BOOTSTRAP_FAILURE-}" = "revision" ] || '
        + '       { [ "${BOOTSTRAP_FAILURE-}" = "target-revision" ] && [[ "$*" != *"-C $DOGFOOD_CLONE_DIR "* ]]; }; '
        + 'then printf "ffffffffffffffffffffffffffffffffffffffff\\n"; '
        + 'else printf "%s\\n" "$DOGFOOD_REPOSITORY_REVISION"; fi ;;\n'
        + '  *"rev-parse HEAD"*) printf "%s\\n" "$DOGFOOD_REPOSITORY_REVISION" ;;\n'
        + '  *"symbolic-ref --quiet HEAD"*) [ "${BOOTSTRAP_FAILURE-}" = "branch" ] ;;\n'
        + '  *"status --porcelain"*) if [ "${BOOTSTRAP_FAILURE-}" = "dirty" ]; then printf " M modified\\n"; fi ;;\n'
        + 'esac\n',
    )
    return bin_dir, call_log
