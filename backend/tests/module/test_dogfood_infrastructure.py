from __future__ import annotations

import configparser
import os
import re
import signal
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.model_settings import OLLAMA_MODEL_NAME

from tests.dogfood_infrastructure_test_support import render_nondefault_dogfood_assets


ROOT_DIR = Path(__file__).parent.parent.parent.parent
DOGFOOD_INFRA_DIR = ROOT_DIR / "infra" / "dogfood"
DOGFOOD_SCRIPTS_DIR = ROOT_DIR / "scripts" / "dogfood"
ENV_EXAMPLE_PATH = DOGFOOD_INFRA_DIR / "env.example"
README_PATH = DOGFOOD_INFRA_DIR / "README.md"
REQUIRED_ENV_KEYS = {
    "DS_ENVIRONMENT_ID",
    "DOGFOOD_WSL_DISTRO",
    "DOGFOOD_SERVICE_USER",
    "DOGFOOD_SERVICE_GROUP",
    "DOGFOOD_REPOSITORY_URL",
    "DOGFOOD_CLONE_DIR",
    "DOGFOOD_CONFIG_DIR",
    "DS_DATA_DIR",
    "DOGFOOD_SERVICE_HOME_DIR",
    "DOGFOOD_OLLAMA_MODELS_DIR",
    "DOGFOOD_BACKUP_DIR",
    "DOGFOOD_BACKUP_RETENTION_COUNT",
    "DOGFOOD_BACKUP_AUTHENTICATION_KEY",
    "DOGFOOD_STATE_DIR",
    "DOGFOOD_LOG_DIR",
    "DOGFOOD_VOICEVOX_IMAGE",
    "DOGFOOD_VOICEVOX_CONTAINER",
}
SHELL_ENTRYPOINTS = (
    "bootstrap.sh",
    "deploy.sh",
    "rollback.sh",
    "start-services.sh",
    "stop-services.sh",
    "restart-services.sh",
    "status.sh",
)


def _read_environment_example() -> dict[str, str]:
    assert ENV_EXAMPLE_PATH.is_file(), "dogfood environment example is required"
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        assert separator, f"環境設定例の行がKEY=VALUE形式ではありません: {line}"
        assert key not in values, f"環境設定キーが重複しています: {key}"
        values[key] = value.strip("'\"")
    return values


def _read_unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


def _assert_finite_stop_timeout(service: configparser.SectionProxy) -> None:
    timeout = service["TimeoutStopSec"]

    assert re.fullmatch(r"[1-9]\d*(?:ms|s|min)?", timeout)


def _run_compose_command(
    command: list[str], environment: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        raise
    assert process.returncode is not None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def test_should_define_separate_dogfood_identity_clone_and_runtime_paths() -> None:
    values = _read_environment_example()

    assert REQUIRED_ENV_KEYS <= values.keys()
    assert values["DS_ENVIRONMENT_ID"] == "dogfood"
    assert values["DOGFOOD_WSL_DISTRO"] == "Ubuntu-dogfood"
    assert values["DOGFOOD_SERVICE_USER"]
    assert values["DOGFOOD_SERVICE_GROUP"]
    assert values["DOGFOOD_SERVICE_HOME_DIR"] == "/var/lib/digital-souls/home"
    assert values["DOGFOOD_OLLAMA_MODELS_DIR"] == (
        "/var/lib/digital-souls/models/ollama"
    )
    assert not values["DOGFOOD_REPOSITORY_URL"].startswith(("/", "file:"))
    assert "DOGFOOD_REPOSITORY_REVISION" not in values

    path_keys = (
        "DOGFOOD_CLONE_DIR",
        "DOGFOOD_CONFIG_DIR",
        "DS_DATA_DIR",
        "DOGFOOD_SERVICE_HOME_DIR",
        "DOGFOOD_OLLAMA_MODELS_DIR",
        "DOGFOOD_BACKUP_DIR",
        "DOGFOOD_STATE_DIR",
        "DOGFOOD_LOG_DIR",
    )
    paths = {key: Path(values[key]) for key in path_keys}
    assert all(path.is_absolute() for path in paths.values())
    assert len(set(paths.values())) == len(paths)
    assert all(
        not path.is_relative_to(ROOT_DIR) and not ROOT_DIR.is_relative_to(path)
        for path in paths.values()
    )
    clone_dir = paths["DOGFOOD_CLONE_DIR"]
    assert all(
        not path.is_relative_to(clone_dir) and not clone_dir.is_relative_to(path)
        for key, path in paths.items()
        if key != "DOGFOOD_CLONE_DIR"
    )


def test_should_keep_dogfood_shell_entrypoints_executable_strict_and_syntax_valid() -> (
    None
):
    paths = tuple(DOGFOOD_SCRIPTS_DIR / name for name in SHELL_ENTRYPOINTS)

    for path in paths:
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        assert os.access(path, os.X_OK)
        assert "set -euo pipefail" in content

    result = subprocess.run(
        ["bash", "-n", *map(str, paths)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_should_preserve_wsl_identity_in_direct_sudo_runbook_commands() -> None:
    source = README_PATH.read_text(encoding="utf-8")

    assert not re.search(r"(?m)^\s*sudo scripts/dogfood/", source)
    for script_name in SHELL_ENTRYPOINTS:
        if f"scripts/dogfood/{script_name}" in source:
            assert (
                f"sudo env WSL_DISTRO_NAME=Ubuntu-dogfood "
                f"scripts/dogfood/{script_name}"
            ) in source


def test_should_apply_declared_ownership_and_restricted_directory_permissions() -> None:
    bootstrap_path = DOGFOOD_SCRIPTS_DIR / "bootstrap.sh"
    assert bootstrap_path.is_file(), "dogfood bootstrap entrypoint is required"
    source = bootstrap_path.read_text(encoding="utf-8")
    shell_sources = "\n".join(
        (DOGFOOD_SCRIPTS_DIR / name).read_text(encoding="utf-8")
        for name in SHELL_ENTRYPOINTS
    )

    for variable in (
        "DOGFOOD_SERVICE_USER",
        "DOGFOOD_SERVICE_GROUP",
        "DOGFOOD_CLONE_DIR",
        "DOGFOOD_CONFIG_DIR",
        "DS_DATA_DIR",
        "DOGFOOD_BACKUP_DIR",
        "DOGFOOD_STATE_DIR",
        "DOGFOOD_LOG_DIR",
    ):
        assert f"${{{variable}}}" in source or f"${variable}" in source
    assert "chown" in source or "install -d" in source
    assert re.search(r"(?:-m|chmod)\s+0?7[0-7][0-7]", source)
    assert all(
        not line.strip().startswith("eval ") for line in shell_sources.splitlines()
    )


def test_should_separate_application_identity_from_docker_operations() -> None:
    source = (DOGFOOD_SCRIPTS_DIR / "bootstrap.sh").read_text(encoding="utf-8")

    assert "runuser" not in source
    assert re.search(
        r'gpasswd --delete "\$DOGFOOD_SERVICE_USER" docker',
        source,
    )
    assert re.search(
        r'chown -R "root:\$DOGFOOD_SERVICE_GROUP" "\$DOGFOOD_CLONE_DIR"', source
    )
    assert re.search(r'chmod -R g-w,o-rwx "\$DOGFOOD_CLONE_DIR"', source)
    assert re.search(r'chmod 0750 "\$DOGFOOD_CLONE_DIR"', source)


def test_should_configure_ollama_systemd_ownership_from_the_shared_environment(
    tmp_path: Path,
) -> None:
    values, generated_dir = render_nondefault_dogfood_assets(tmp_path)
    unit_path = generated_dir / "digital-souls-ollama.service"
    unit = _read_unit(unit_path)

    assert unit["Service"]["User"] == values["DOGFOOD_SERVICE_USER"]
    assert unit["Service"]["Group"] == values["DOGFOOD_SERVICE_GROUP"]
    assert unit["Service"]["EnvironmentFile"] == (
        f"{values['DOGFOOD_CONFIG_DIR']}/dogfood.env"
    )
    assert unit["Service"]["Restart"] == "on-failure"
    assert "ExecStart" in unit["Service"]
    assert unit["Service"]["ExecStart"] == (
        f"{values['DOGFOOD_CLONE_DIR']}/scripts/dogfood/run-ollama.sh"
    )
    environment = unit["Service"]["Environment"]
    assert f"DOGFOOD_ENV_FILE={values['DOGFOOD_CONFIG_DIR']}/dogfood.env" in environment
    assert f"WSL_DISTRO_NAME={values['DOGFOOD_WSL_DISTRO']}" in environment
    assert "PartOf" in unit["Unit"]
    assert "After" in unit["Unit"]
    assert "target" in unit["Unit"]["PartOf"]


def test_should_stop_ollama_with_sigterm_before_a_finite_timeout(
    tmp_path: Path,
) -> None:
    _, generated_dir = render_nondefault_dogfood_assets(tmp_path)
    unit_path = generated_dir / "digital-souls-ollama.service"
    service = _read_unit(unit_path)["Service"]

    assert service["KillSignal"] == "SIGTERM"
    _assert_finite_stop_timeout(service)


def test_should_configure_voicevox_systemd_compose_lifecycle(
    tmp_path: Path,
) -> None:
    values, generated_dir = render_nondefault_dogfood_assets(tmp_path)
    unit_path = generated_dir / "digital-souls-voicevox.service"
    unit = _read_unit(unit_path)
    service = unit["Service"]

    assert service["User"] == "root"
    assert service["Group"] == "root"
    assert service["EnvironmentFile"] == f"{values['DOGFOOD_CONFIG_DIR']}/dogfood.env"
    assert "compose" in service["ExecStart"] and " up " in service["ExecStart"]
    assert service["ExecStart"].startswith(values["DOGFOOD_CLONE_DIR"])
    assert service["ExecStop"].startswith(values["DOGFOOD_CLONE_DIR"])
    assert service["RemainAfterExit"] == "yes"
    assert "Restart" not in service
    assert "RestartSec" not in service
    assert "PartOf" in unit["Unit"]


def test_should_stop_voicevox_compose_before_a_finite_timeout(
    tmp_path: Path,
) -> None:
    _, generated_dir = render_nondefault_dogfood_assets(tmp_path)
    unit_path = generated_dir / "digital-souls-voicevox.service"
    service = _read_unit(unit_path)["Service"]

    assert "compose" in service["ExecStop"]
    assert "down" in service["ExecStop"].split()
    _assert_finite_stop_timeout(service)


def test_should_define_one_inference_target_for_both_owned_services() -> None:
    target_path = DOGFOOD_INFRA_DIR / "systemd" / "digital-souls-inference.target"
    target = _read_unit(target_path)
    service_names = {
        "digital-souls-ollama.service",
        "digital-souls-voicevox.service",
    }

    wants = set(target["Unit"]["Wants"].split())

    assert wants == service_names


def test_should_route_service_lifecycle_entrypoints_through_the_dogfood_target() -> (
    None
):
    target_name = "digital-souls-dogfood.target"

    for script_name, action in (
        ("start-services.sh", "start"),
        ("stop-services.sh", "stop"),
        ("restart-services.sh", "restart"),
    ):
        source = (DOGFOOD_SCRIPTS_DIR / script_name).read_text(encoding="utf-8")
        systemctl_lines = [
            line.strip()
            for line in source.splitlines()
            if "systemctl" in line and not line.lstrip().startswith("#")
        ]

        assert len(systemctl_lines) == 1
        assert action in systemctl_lines[0].split()
        assert target_name in systemctl_lines[0]


def test_should_limit_compose_to_loopback_voicevox_only() -> None:
    compose_paths = tuple((DOGFOOD_INFRA_DIR / "voicevox").glob("*.y*ml"))
    assert len(compose_paths) == 1
    content = compose_paths[0].read_text(encoding="utf-8")
    service_block = re.search(
        r"(?ms)^services:\s*\n(?P<body>(?:^[ \t]+.*\n?)*)",
        content,
    )
    assert service_block is not None
    service_names = re.findall(r"(?m)^  ([A-Za-z0-9_.-]+):\s*$", service_block["body"])

    assert service_names == ["voicevox"]
    assert "${VOICEVOX_HOST}" in content
    assert "${VOICEVOX_PORT}" in content
    assert "0.0.0.0" not in content
    assert not re.search(r"(?m)^  (frontend|backend|ollama):", service_block["body"])


def test_should_mount_only_voicevox_local_share_as_tmpfs() -> None:
    compose_path = DOGFOOD_INFRA_DIR / "voicevox" / "compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    service = compose["services"]["voicevox"]
    tmpfs = service.get("tmpfs")

    assert tmpfs == ["/home/user/.local/share:uid=1000,gid=1000,size=64m"]
    assert "user" not in service
    assert tmpfs is not None
    assert all(entry.split(":", maxsplit=1)[0] != "/home/user" for entry in tmpfs)
    assert "UID/GID 1000" in compose_path.read_text(encoding="utf-8")


def test_should_keep_voicevox_cpu_runtime_and_existing_user_package_available(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLIが利用できないためVOICEVOX runtime検証を省略します")
    daemon = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
    )
    if daemon.returncode != 0:
        pytest.skip("Docker daemonが利用できないためVOICEVOX runtime検証を省略します")
    compose_path = DOGFOOD_INFRA_DIR / "voicevox" / "compose.yaml"
    project = re.sub(r"[^a-z0-9_-]", "-", f"voicevox-{tmp_path.name}".lower())
    compose_command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--file",
        str(compose_path),
    ]
    environment = {
        **os.environ,
        "DOGFOOD_VOICEVOX_IMAGE": "voicevox/voicevox_engine:cpu-ubuntu20.04-latest",
        "DOGFOOD_VOICEVOX_CONTAINER": project,
        "VOICEVOX_HOST": "127.0.0.1",
        "VOICEVOX_PORT": "0",
    }
    image = subprocess.run(
        ["docker", "image", "inspect", environment["DOGFOOD_VOICEVOX_IMAGE"]],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if image.returncode != 0:
        pytest.skip("VOICEVOX imageが未取得のためruntime検証を省略します")

    try:
        started = _run_compose_command(
            [*compose_command, "up", "--detach"],
            environment,
            timeout=120,
        )
        assert started.returncode == 0, (started.stdout, started.stderr)
        running = _run_compose_command(
            [*compose_command, "ps", "--status", "running", "--services"],
            environment,
            timeout=10,
        )
        assert running.returncode == 0, (running.stdout, running.stderr)
        assert running.stdout.splitlines() == ["voicevox"]
        published = _run_compose_command(
            [*compose_command, "port", "voicevox", "50021"],
            environment,
            timeout=10,
        )
        assert published.returncode == 0, (published.stdout, published.stderr)
        published_port = published.stdout.strip().rsplit(":", maxsplit=1)[-1]
        assert published_port.isdigit(), published.stdout
        from http_readiness import wait_for_http

        readiness = wait_for_http(
            f"http://127.0.0.1:{published_port}/version",
            max_attempts=120,
            interval_seconds=0.5,
            request_timeout_seconds=1.0,
        )
        assert readiness.result == "ready", readiness
        package = _run_compose_command(
            [
                *compose_command,
                "exec",
                "--no-TTY",
                "--user",
                "user",
                "voicevox",
                "/opt/python/bin/python3",
                "-c",
                "import pyopenjtalk; print(pyopenjtalk.__file__)",
            ],
            environment,
            timeout=30,
        )
        assert package.returncode == 0, (package.stdout, package.stderr)
        assert "/home/user/.local/lib/" in package.stdout
        logs = _run_compose_command(
            [*compose_command, "logs", "--no-color", "voicevox"],
            environment,
            timeout=10,
        )
        assert logs.returncode == 0, (logs.stdout, logs.stderr)
        assert "PermissionError" not in f"{logs.stdout}\n{logs.stderr}"
        still_running = _run_compose_command(
            [*compose_command, "ps", "--status", "running", "--services"],
            environment,
            timeout=10,
        )
        assert still_running.returncode == 0, (
            still_running.stdout,
            still_running.stderr,
        )
        assert still_running.stdout.splitlines() == ["voicevox"]
    finally:
        _run_compose_command(
            [*compose_command, "down", "--volumes", "--remove-orphans"],
            environment,
            timeout=30,
        )


def test_should_delegate_voicevox_container_recovery_to_compose() -> None:
    compose_path = DOGFOOD_INFRA_DIR / "voicevox" / "compose.yaml"
    content = compose_path.read_text(encoding="utf-8")

    assert re.search(r'(?m)^    restart:\s+["\']?unless-stopped["\']?\s*$', content)


def test_should_preserve_all_placeholders_when_rendering_sed_metacharacters(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "generated"
    output_dir.mkdir()
    values = {
        "DOGFOOD_SERVICE_USER": r"service&user|segment\leaf",
        "DOGFOOD_SERVICE_GROUP": r"service&group|segment\leaf",
        "DOGFOOD_CONFIG_DIR": r"/srv/config&dog|segment\leaf",
        "DOGFOOD_CLONE_DIR": r"/srv/clone&dog|segment\leaf",
        "DOGFOOD_WSL_DISTRO": r"Ubuntu&dogfood|segment\leaf",
        "DOGFOOD_SERVICE_HOME_DIR": r"/srv/home&food|segment\leaf",
        "DS_DATA_DIR": r"/srv/dog&food|segment\leaf",
    }
    result = subprocess.run(
        [
            str(DOGFOOD_SCRIPTS_DIR / "render-assets.sh"),
            str(DOGFOOD_INFRA_DIR / "templates"),
            str(output_dir),
        ],
        env={
            **os.environ,
            **values,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    rendered = "\n".join(
        path.read_text(encoding="utf-8") for path in output_dir.iterdir()
    )
    assert all(value in rendered for value in values.values())


def test_should_require_service_home_when_rendering_assets(tmp_path: Path) -> None:
    output_dir = tmp_path / "generated"
    output_dir.mkdir()

    result = subprocess.run(
        [
            str(DOGFOOD_SCRIPTS_DIR / "render-assets.sh"),
            str(DOGFOOD_INFRA_DIR / "templates"),
            str(output_dir),
        ],
        env={
            **os.environ,
            "DOGFOOD_SERVICE_USER": "digital-souls",
            "DOGFOOD_SERVICE_GROUP": "digital-souls",
            "DOGFOOD_CONFIG_DIR": "/etc/digital-souls",
            "DOGFOOD_CLONE_DIR": "/opt/digital-souls/current",
            "DOGFOOD_WSL_DISTRO": "Ubuntu-dogfood",
            "DS_DATA_DIR": "/var/lib/digital-souls",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "DOGFOOD_SERVICE_HOME_DIR" in result.stderr


def test_should_generate_a_windows_entrypoint_from_the_shared_environment(
    tmp_path: Path,
) -> None:
    values, generated_dir = render_nondefault_dogfood_assets(tmp_path)
    source = (generated_dir / "start-dogfood-wsl.ps1").read_text(encoding="utf-8")

    assert "wsl.exe" in source
    assert values["DOGFOOD_WSL_DISTRO"] in source
    assert re.search(r"wsl\.exe\s+.*--user\s+root(?:\s|$)", source)
    assert "systemctl start digital-souls-dogfood.target" in source
    assert "$LASTEXITCODE -ne 0" in source
    assert source.index("wsl.exe") < source.index("$LASTEXITCODE -ne 0")
    assert "throw" in source
    assert "start-services.sh" not in source
    assert not re.search(r"\bsystemctl\s+(?:stop|restart|is-active|show)\b", source)


def test_should_delegate_application_lifecycle_to_one_oneshot_systemd_unit(
    tmp_path: Path,
) -> None:
    values, generated_dir = render_nondefault_dogfood_assets(tmp_path)
    unit_path = generated_dir / "digital-souls-application.service"
    assert unit_path.is_file()
    unit = _read_unit(unit_path)
    service = unit["Service"]

    assert service["Type"] == "oneshot"
    assert service["RemainAfterExit"] == "yes"
    assert service["User"] == values["DOGFOOD_SERVICE_USER"]
    assert service["Group"] == values["DOGFOOD_SERVICE_GROUP"]
    assert service["ExecStart"] == f"{values['DOGFOOD_CLONE_DIR']}/environments/up.sh"
    assert service["ExecStop"] == f"{values['DOGFOOD_CLONE_DIR']}/environments/down.sh"
    assert "EnvironmentFile" not in service
    assert "DS_ENVIRONMENT_ID=dogfood" in service["Environment"]
    assert f"DS_DATA_DIR={values['DS_DATA_DIR']}" in service["Environment"]
    assert f"HOME={values['DOGFOOD_SERVICE_HOME_DIR']}" in service["Environment"]
    assert f"XDG_CACHE_HOME={values['DS_DATA_DIR']}/cache" in service["Environment"]
    assert (
        f"npm_config_cache={values['DS_DATA_DIR']}/cache/npm" in service["Environment"]
    )
    assert "DOGFOOD_BACKUP_AUTHENTICATION_KEY" not in unit_path.read_text(
        encoding="utf-8"
    )
    assert "digital-souls-inference.target" in unit["Unit"]["After"].split()
    assert "Restart" not in service


def test_should_document_executable_pull_command_for_backend_default_model() -> None:
    source = README_PATH.read_text(encoding="utf-8")
    command_blocks = re.findall(r"```bash\n(.*?)```", source, flags=re.DOTALL)
    pull_commands = [
        " ".join(line.rstrip("\\").strip() for line in block.splitlines())
        for block in command_blocks
        if "ollama pull" in block
    ]

    expected_parts = (
        "sudo -u digital-souls env",
        "HOME=/var/lib/digital-souls/home",
        "OLLAMA_MODELS=/var/lib/digital-souls/models/ollama",
        f"ollama pull {OLLAMA_MODEL_NAME}",
    )
    assert any(
        all(part in command for part in expected_parts) for command in pull_commands
    )


def test_should_document_executable_commands_when_running_restore_drill() -> None:
    source = README_PATH.read_text(encoding="utf-8")
    drill_section = source.split("### Issue #56 restore drill", maxsplit=1)[1].split(
        "\n## ", maxsplit=1
    )[0]
    command_blocks = re.findall(r"```bash\n(.*?)```", drill_section, flags=re.DOTALL)
    commands: list[str] = []
    for block in command_blocks:
        continued_lines: list[str] = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            continued_lines.append(stripped.rstrip("\\").rstrip())
            if not stripped.endswith("\\"):
                commands.append(" ".join(continued_lines))
                continued_lines = []
        assert continued_lines == []

    install_command = next(command for command in commands if "install -d" in command)
    init_command = next(
        command for command in commands if "environment_cli.py init-data-root" in command
    )
    restore_command = next(
        command
        for command in commands
        if "environment_cli.py restore " in command
    )
    verify_command = next(
        command
        for command in commands
        if "environment_cli.py restore-verify " in command
    )
    disable_history_command = next(
        command for command in commands if command == "set +o history"
    )
    load_key_command = next(
        command
        for command in commands
        if command.startswith("DOGFOOD_BACKUP_AUTHENTICATION_KEY=$(sudo awk")
    )
    export_key_command = next(
        command
        for command in commands
        if command == "export DOGFOOD_BACKUP_AUTHENTICATION_KEY"
    )
    unset_key_command = next(
        command
        for command in commands
        if command == "unset DOGFOOD_BACKUP_AUTHENTICATION_KEY"
    )
    enable_history_command = next(
        command for command in commands if command == "set -o history"
    )

    assert commands.index(install_command) < commands.index(init_command)
    assert commands.index(init_command) < commands.index(disable_history_command)
    assert commands.index(disable_history_command) < commands.index(load_key_command)
    assert commands.index(load_key_command) < commands.index(export_key_command)
    assert commands.index(export_key_command) < commands.index(restore_command)
    assert commands.index(restore_command) < commands.index(verify_command)
    assert commands.index(verify_command) < commands.index(unset_key_command)
    assert commands.index(unset_key_command) < commands.index(enable_history_command)
    assert install_command == (
        "sudo install -d -m 0750 -o digital-souls -g digital-souls "
        "/var/lib/digital-souls/restore-drill"
    )
    assert "sudo -u digital-souls env" in init_command
    assert "DS_ENVIRONMENT_ID=dogfood" in init_command
    assert "DS_DATA_DIR=/var/lib/digital-souls/restore-drill" in init_command
    assert "/opt/digital-souls/current/backend/.venv/bin/python" in init_command
    assert "--environment dogfood" in init_command
    assert "--repository-root /opt/digital-souls/current" in init_command
    for command in (restore_command, verify_command):
        assert "DS_ENVIRONMENT_ID=dogfood" in command
        assert "DS_DATA_DIR=/var/lib/digital-souls/restore-drill" in command
        assert "--environment dogfood" in command
        assert "--repository-root /opt/digital-souls/current" in command
    assert "/etc/digital-souls/dogfood.env" in load_key_command
    assert "DOGFOOD_BACKUP_AUTHENTICATION_KEY=" in load_key_command
    executable_commands = " ".join(commands)
    assert "python -c" not in executable_commands
    assert "sys.path" not in executable_commands
    assert "initialize_runtime_data_root" not in executable_commands


def test_should_require_inference_and_application_from_dogfood_target() -> None:
    target_path = DOGFOOD_INFRA_DIR / "systemd" / "digital-souls-dogfood.target"
    assert target_path.is_file()
    target = _read_unit(target_path)["Unit"]
    dependencies = {
        "digital-souls-inference.target",
        "digital-souls-application.service",
    }

    assert set(target["Requires"].split()) == dependencies
    assert set(target["After"].split()) == dependencies


def test_should_stop_application_and_inference_with_dogfood_target() -> None:
    application = _read_unit(
        DOGFOOD_INFRA_DIR / "templates" / "digital-souls-application.service.template"
    )["Unit"]
    inference = _read_unit(
        DOGFOOD_INFRA_DIR / "systemd" / "digital-souls-inference.target"
    )["Unit"]

    assert application["PartOf"] == "digital-souls-dogfood.target"
    assert inference["PartOf"] == "digital-souls-dogfood.target"
