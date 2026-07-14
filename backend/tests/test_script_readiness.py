import os
import subprocess
from pathlib import Path

import pytest


_READINESS_LIBRARY = Path(__file__).parent.parent.parent / "scripts/lib/readiness.sh"


def _make_command_stub(bin_dir: Path, name: str, body: str) -> None:
    stub = bin_dir / name
    stub.write_text(f"#!/usr/bin/env bash\n{body}\n")
    stub.chmod(0o755)


def _run_readiness_harness(
    tmp_path: Path,
    body: str,
    *,
    max_attempts: int | str | None = 30,
    interval: str | None = "1",
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
    }
    if max_attempts is not None:
        env["HTTP_READINESS_MAX_ATTEMPTS"] = str(max_attempts)
    if interval is not None:
        env["HTTP_READINESS_INTERVAL_SECONDS"] = interval
    script = f'''set -euo pipefail
source "{_READINESS_LIBRARY}"
{body}
'''
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


class TestWaitForHttp:
    def test_defaults_to_thirty_attempts_one_second_apart(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl_log = tmp_path / "curl.log"
        sleep_log = tmp_path / "sleep.log"
        _make_command_stub(
            bin_dir,
            "curl",
            f'''printf 'attempt\\n' >> "{curl_log}"
exit 22''',
        )
        _make_command_stub(
            bin_dir,
            "sleep",
            f'''printf '%s\\n' "$1" >> "{sleep_log}"''',
        )

        result = _run_readiness_harness(
            tmp_path,
            'wait_for_http "http://service.test/health" "Example"',
            max_attempts=None,
            interval=None,
        )

        assert result.returncode == 1
        assert curl_log.read_text().splitlines() == ["attempt"] * 30
        assert sleep_log.read_text().splitlines() == ["1"] * 29
        assert "ERROR: Example did not become ready within 30 seconds" in result.stderr

    def test_http_success_returns_zero_and_reports_ready(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl_log = tmp_path / "curl.log"
        _make_command_stub(
            bin_dir,
            "curl",
            f'''printf '%s\\n' "$@" > "{curl_log}"
exit 0''',
        )
        _make_command_stub(bin_dir, "sleep", "exit 99")

        result = _run_readiness_harness(
            tmp_path,
            'wait_for_http "http://service.test/health" "Example"',
        )

        assert result.returncode == 0
        assert curl_log.read_text().splitlines() == [
            "-sf",
            "http://service.test/health",
        ]
        assert "Waiting for Example to be ready" in result.stdout
        assert "Example is ready." in result.stdout

    def test_retries_with_configured_count_and_interval_before_success(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl_log = tmp_path / "curl.log"
        sleep_log = tmp_path / "sleep.log"
        _make_command_stub(
            bin_dir,
            "curl",
            f'''printf 'attempt\\n' >> "{curl_log}"
[ "$(wc -l < "{curl_log}")" -ge 3 ]''',
        )
        _make_command_stub(
            bin_dir,
            "sleep",
            f'''printf '%s\\n' "$1" >> "{sleep_log}"''',
        )

        result = _run_readiness_harness(
            tmp_path,
            'wait_for_http "http://service.test/health" "Example"',
            max_attempts=3,
            interval="0.25",
        )

        assert result.returncode == 0
        assert curl_log.read_text().splitlines() == ["attempt"] * 3
        assert sleep_log.read_text().splitlines() == ["0.25", "0.25"]

    def test_timeout_after_exact_attempt_limit_without_trailing_sleep(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl_log = tmp_path / "curl.log"
        sleep_log = tmp_path / "sleep.log"
        _make_command_stub(
            bin_dir,
            "curl",
            f'''printf 'attempt\\n' >> "{curl_log}"
exit 22''',
        )
        _make_command_stub(
            bin_dir,
            "sleep",
            f'''printf '%s\\n' "$1" >> "{sleep_log}"''',
        )

        result = _run_readiness_harness(
            tmp_path,
            'wait_for_http "http://service.test/health" "Example"',
            max_attempts=1,
        )

        assert result.returncode == 1
        assert curl_log.read_text().splitlines() == ["attempt"]
        assert not sleep_log.exists()
        assert "ERROR: Example did not become ready within 1 seconds" in result.stderr

    def test_timeout_preserves_existing_message_for_configured_attempt_limit(
        self, tmp_path
    ):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _make_command_stub(bin_dir, "curl", "exit 22")
        _make_command_stub(bin_dir, "sleep", "exit 0")

        result = _run_readiness_harness(
            tmp_path,
            'wait_for_http "http://service.test/health" "Example"',
            max_attempts=2,
            interval="0.25",
        )

        assert result.returncode == 1
        assert "ERROR: Example did not become ready within 2 seconds" in result.stderr

    @pytest.mark.parametrize(
        ("max_attempts", "interval", "expected_setting"),
        [
            ("invalid", "1", "HTTP_READINESS_MAX_ATTEMPTS"),
            ("", "1", "HTTP_READINESS_MAX_ATTEMPTS"),
            (0, "1", "HTTP_READINESS_MAX_ATTEMPTS"),
            (2, "invalid", "HTTP_READINESS_INTERVAL_SECONDS"),
            (2, "", "HTTP_READINESS_INTERVAL_SECONDS"),
            (2, "-1", "HTTP_READINESS_INTERVAL_SECONDS"),
        ],
    )
    def test_invalid_readiness_config_fails_before_http_attempt(
        self, tmp_path, max_attempts, interval, expected_setting
    ):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl_log = tmp_path / "curl.log"
        _make_command_stub(bin_dir, "curl", f'''touch "{curl_log}"''')
        _make_command_stub(bin_dir, "sleep", "exit 0")

        result = _run_readiness_harness(
            tmp_path,
            'wait_for_http "http://service.test/health" "Example"',
            max_attempts=max_attempts,
            interval=interval,
        )

        assert result.returncode == 1
        assert expected_setting in result.stderr
        assert not curl_log.exists()

    def test_oversized_max_attempts_fails_before_http_attempt(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl_log = tmp_path / "curl.log"
        _make_command_stub(bin_dir, "curl", f'''touch "{curl_log}"''')
        _make_command_stub(bin_dir, "sleep", "exit 0")

        result = _run_readiness_harness(
            tmp_path,
            'wait_for_http "http://service.test/health" "Example"',
            max_attempts="999999999999999999999999999999",
        )

        assert result.returncode == 1
        assert "must be an integer between 1 and 999999999" in result.stderr
        assert not curl_log.exists()

    def test_related_process_failure_preserves_child_status(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _make_command_stub(bin_dir, "curl", "exit 22")
        _make_command_stub(bin_dir, "sleep", "exit 99")

        result = _run_readiness_harness(
            tmp_path,
            '''bash -c 'exit 7' &
child_pid="$!"
/bin/sleep 0.05
wait_for_http "http://service.test/health" "Example" "$child_pid"''',
        )

        assert result.returncode == 7
        assert "Example process exited before becoming ready" in result.stderr
        assert "did not become ready" not in result.stderr

    def test_related_process_normal_exit_is_normalized_to_one(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _make_command_stub(bin_dir, "curl", "exit 22")
        _make_command_stub(bin_dir, "sleep", "exit 99")

        result = _run_readiness_harness(
            tmp_path,
            '''bash -c 'exit 0' &
child_pid="$!"
/bin/sleep 0.05
wait_for_http "http://service.test/health" "Example" "$child_pid"''',
        )

        assert result.returncode == 1
        assert "Example process exited before becoming ready" in result.stderr

    def test_omitted_related_pid_uses_timeout_path(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _make_command_stub(bin_dir, "curl", "exit 22")
        _make_command_stub(bin_dir, "sleep", "exit 0")

        result = _run_readiness_harness(
            tmp_path,
            'wait_for_http "http://service.test/health" "Example"',
            max_attempts=2,
        )

        assert result.returncode == 1
        assert "did not become ready" in result.stderr
        assert "process exited" not in result.stderr
