import os
import signal
import subprocess
import time
from pathlib import Path

import pytest


_PROCESS_LIBRARY = Path(__file__).parent.parent.parent / "scripts/lib/process.sh"


def _wait_until_process_exits(pid: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists() and stat_path.read_text().split()[2] == "Z":
            return
        time.sleep(0.01)
    pytest.fail(f"process {pid} remained alive after cleanup")


def _run_process_harness(
    body: str, *, input_text: str | None = None, timeout: int = 10
) -> subprocess.CompletedProcess:
    script = f'''set -euo pipefail
source "{_PROCESS_LIBRARY}"
{body}
'''
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        input=input_text,
        text=True,
        timeout=timeout,
    )


class TestProcessWait:
    def test_sourcing_library_does_not_register_traps(self):
        result = _run_process_harness("trap -p EXIT INT TERM\n")

        assert result.returncode == 0
        assert result.stdout == ""

    def test_single_child_normal_exit_returns_zero(self):
        result = _run_process_harness(
            '''process_manager_init
process_start_child "child" bash -c 'exit 0'
process_wait_all
'''
        )

        assert result.returncode == 0

    def test_single_child_failure_preserves_exit_code(self):
        result = _run_process_harness(
            '''process_manager_init
process_start_child "child" bash -c 'exit 7'
process_wait_all
'''
        )

        assert result.returncode == 7

    def test_multiple_children_expose_distinct_registered_pids(self):
        result = _run_process_harness(
            '''process_manager_init
process_start_child "first" sleep 30
first_pid="$(process_last_started_pid)"
process_start_child "second" sleep 30
second_pid="$(process_last_started_pid)"
printf '%s\n%s\n' "$first_pid" "$second_pid"
process_cleanup
'''
        )

        assert result.returncode == 0
        pids = [int(line) for line in result.stdout.splitlines() if line.isdigit()]
        assert len(pids) == 2
        assert pids[0] != pids[1]
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)

    def test_pid_result_does_not_depend_on_caller_variable_name(self):
        result = _run_process_harness(
            '''process_manager_init
registered_pid=unchanged
process_start_child "child" sleep 30
printf '%s\n%s\n' "$registered_pid" "$(process_last_started_pid)"
process_cleanup
'''
        )

        assert result.returncode == 0
        result_lines = result.stdout.splitlines()
        assert result_lines[-2] == "unchanged"
        assert result_lines[-1].isdigit()

    def test_multiple_children_return_last_observed_nonzero_status(self):
        result = _run_process_harness(
            '''process_manager_init
process_start_child "first" bash -c 'exit 3'
process_start_child "second" bash -c 'exit 0'
process_start_child "third" bash -c 'exit 9'
process_wait_all
'''
        )

        assert result.returncode == 9

    def test_started_child_does_not_consume_parent_stdin(self):
        result = _run_process_harness(
            '''process_manager_init
process_start_child "child" bash -c 'if IFS= read -r line; then printf "shared-read=%s\\n" "$line"; else printf "child-eof\\n"; fi'
process_wait_all
''',
            input_text="input-line\n",
        )

        assert result.returncode == 0
        assert result.stdout.splitlines()[-1] == "child-eof"

    def test_started_child_does_not_leak_stdout_fd_to_grandchild(self, tmp_path):
        grandchild_pid_file = tmp_path / "grandchild.pid"
        script = f'''set -euo pipefail
source "{_PROCESS_LIBRARY}"
process_manager_init
process_start_child "child" bash -c 'sleep 30 >/dev/null 2>&1 & printf "%s\\n" "$!" > "{grandchild_pid_file}"'
process_wait_all
'''
        process = subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            assert process.wait(timeout=5) == 0
            assert process.stdout is not None
            os.set_blocking(process.stdout.fileno(), False)
            stdout = os.read(process.stdout.fileno(), 4096)
            assert stdout == b"==> Starting child...\n"
            assert os.read(process.stdout.fileno(), 4096) == b""
        finally:
            if grandchild_pid_file.exists():
                grandchild_pid = int(grandchild_pid_file.read_text())
                try:
                    os.kill(grandchild_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


class TestProcessCleanup:
    @pytest.mark.parametrize(
        ("received_signal", "expected_code"),
        [("INT", 130), ("TERM", 143)],
    )
    def test_signal_before_pid_registration_cleans_up_child(
        self, tmp_path, received_signal, expected_code
    ):
        child_pid_file = tmp_path / "child.pid"
        script = f'''set -euo pipefail
source "{_PROCESS_LIBRARY}"
process_manager_init
parent_pid="$BASHPID"
set -T
trap 'if [ "$BASHPID" = "$parent_pid" ] && [[ "$BASH_COMMAND" == PROCESS_MANAGER_PIDS+* ]]; then
  trap - DEBUG
  builtin printf "%s\\n" "$group_pid" > "{child_pid_file}"
  while [ "$PROCESS_MANAGER_PENDING_SIGNAL" -eq 0 ]; do
    :
  done
fi' DEBUG
process_start_child "child" sleep 30
'''
        process = subprocess.Popen(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid_file.exists()

        process.send_signal(getattr(signal, f"SIG{received_signal}"))
        process.communicate(timeout=5)

        assert process.returncode == expected_code
        child_pid = int(child_pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_cleanup_sends_term_in_registration_order(self, tmp_path):
        signal_log = tmp_path / "signals.log"
        result = _run_process_harness(
            f'''process_manager_init
kill() {{
  if [ "$1" = "-0" ]; then
    builtin kill "$@"
  else
    printf '%s\n' "${{@: -1}}" >> "{signal_log}"
    builtin kill "$@"
  fi
}}
process_start_child "first" sleep 30
first_pid="$(process_last_started_pid)"
process_start_child "second" sleep 30
second_pid="$(process_last_started_pid)"
printf '%s\n%s\n' "$first_pid" "$second_pid"
process_cleanup
'''
        )

        assert result.returncode == 0
        registered_pids = [line for line in result.stdout.splitlines() if line.isdigit()]
        assert signal_log.read_text().splitlines() == [
            f"-{pid}" for pid in registered_pids
        ]

    def test_cleanup_is_idempotent(self, tmp_path):
        signal_log = tmp_path / "signals.log"
        result = _run_process_harness(
            f'''process_manager_init
kill() {{
  if [ "$1" = "-0" ]; then
    builtin kill "$@"
  else
    printf '%s\n' "$1" >> "{signal_log}"
    builtin kill "$@"
  fi
}}
process_start_child "child" sleep 30
process_cleanup
process_cleanup
'''
        )

        assert result.returncode == 0
        assert len(signal_log.read_text().splitlines()) == 1

    def test_exit_trap_cleans_up_registered_child(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        result = _run_process_harness(
            f'''process_manager_init
process_start_child "child" sleep 30
child_pid="$(process_last_started_pid)"
printf '%s\n' "$child_pid" > "{pid_file}"
exit 0
'''
        )

        assert result.returncode == 0
        child_pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)

    @pytest.mark.parametrize(
        ("received_signal", "expected_code"),
        [("INT", 130), ("TERM", 143)],
    )
    def test_signal_cleans_up_with_conventional_exit_code(
        self, tmp_path, received_signal, expected_code
    ):
        child_pid_file = tmp_path / "child.pid"
        result = _run_process_harness(
            f'''process_manager_init
process_start_child "child" sleep 30
child_pid="$(process_last_started_pid)"
printf '%s\n' "$child_pid" > "{child_pid_file}"
kill -{received_signal} $$
'''
        )

        assert result.returncode == expected_code
        child_pid = int(child_pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)

    @pytest.mark.parametrize(
        ("shutdown_command", "expected_code"),
        [("exit 0", 0), ("kill -INT $$", 130), ("kill -TERM $$", 143)],
    )
    def test_cleanup_terminates_descendant_processes(
        self, tmp_path, shutdown_command, expected_code
    ):
        descendant_pid_file = tmp_path / "descendant.pid"
        result = _run_process_harness(
            f'''process_manager_init
process_start_child "child" bash -c 'sleep 30 >/dev/null 2>&1 & printf "%s\\n" "$!" > "{descendant_pid_file}"'
while [ ! -s "{descendant_pid_file}" ]; do
  :
done
process_wait_all
{shutdown_command}
'''
        )

        assert result.returncode == expected_code
        descendant_pid = int(descendant_pid_file.read_text())
        _wait_until_process_exits(descendant_pid)
