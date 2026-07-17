from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import tests.environment_test_support


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    pytest.fail(f"{path} was not created")


def test_should_start_command_in_its_own_process_group_with_identity(tmp_path: Path):
    from process_control import start_managed_process

    handle = start_managed_process(
        label="backend",
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        env=dict(os.environ),
    )
    try:
        assert handle.identity.pid == handle.process.pid
        assert handle.identity.pgid == handle.identity.pid
        assert handle.identity.session_id == handle.identity.pid
        assert handle.identity.start_time > 0
    finally:
        os.killpg(handle.identity.pgid, signal.SIGKILL)
        handle.process.wait(timeout=5)


def test_should_not_share_parent_standard_input(tmp_path: Path):
    from process_control import start_managed_process

    output = tmp_path / "stdin.txt"
    handle = start_managed_process(
        label="frontend",
        command=(
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(output)!r}).write_text(input())",
        ),
        cwd=tmp_path,
        env=dict(os.environ),
    )

    returncode = handle.process.wait(timeout=5)

    assert returncode != 0
    assert not output.exists()


def test_should_terminate_child_when_identity_cannot_be_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import process_control

    started: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(process_control.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(
        process_control,
        "_read_identity",
        lambda pid: (_ for _ in ()).throw(OSError("proc unavailable")),
    )

    with pytest.raises(RuntimeError, match="identity could be recorded"):
        process_control.start_managed_process(
            label="backend",
            command=(sys.executable, "-c", "import time; time.sleep(30)"),
            cwd=tmp_path,
            env=dict(os.environ),
        )

    assert len(started) == 1
    assert started[0].poll() is not None
    assert process_control._process_group_has_live_members(started[0].pid) is False


def test_should_report_failed_identity_rollback_when_group_survives_kill(
    monkeypatch: pytest.MonkeyPatch,
):
    import process_control

    process = subprocess.Popen[bytes](
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
    )
    process.wait(timeout=5)
    waits = iter((False, False))
    sent: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        process_control,
        "_wait_until_process_group_exits",
        lambda pgid, deadline: next(waits),
    )
    monkeypatch.setattr(
        process_control,
        "_signal_new_process_group",
        lambda pgid, sent_signal: sent.append((pgid, sent_signal)),
    )

    with pytest.raises(RuntimeError, match="remained alive after SIGKILL"):
        process_control._rollback_unidentified_process(process)

    assert sent == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]


def test_should_detect_start_time_mismatch_for_reused_pid():
    from process_control import ProcessIdentity, process_identity_matches

    actual_start_time = int(Path(f"/proc/{os.getpid()}/stat").read_text().split()[21])
    stale = ProcessIdentity(
        pid=os.getpid(),
        pgid=os.getpgid(0),
        session_id=os.getsid(0),
        start_time=actual_start_time + 1,
    )

    assert process_identity_matches(stale) is False


def test_should_refuse_to_signal_stale_process_identity(monkeypatch: pytest.MonkeyPatch):
    from process_control import ProcessIdentity, stop_owned_process

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    stale = ProcessIdentity(pid=999999, pgid=999999, session_id=999999, start_time=1)

    result = stop_owned_process(stale, grace_seconds=0.01)

    assert result.result == "skipped_identity_mismatch"
    assert sent == []


def test_should_refuse_orphan_group_with_members_older_than_recorded_leader(
    monkeypatch: pytest.MonkeyPatch,
):
    import process_control
    from process_control import ProcessIdentity, stop_owned_process

    stale = ProcessIdentity(pid=91, pgid=91, session_id=91, start_time=200)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(process_control, "_leader_identity_matches", lambda identity: False)
    monkeypatch.setattr(
        process_control,
        "_process_stat",
        lambda pid: (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setattr(process_control, "_group_members", lambda identity: ((92, 199),))
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))

    result = stop_owned_process(stale, grace_seconds=0.01)

    assert result.result == "skipped_identity_mismatch"
    assert sent == []


def test_should_refuse_reported_group_when_recorded_leader_has_exited(
    monkeypatch: pytest.MonkeyPatch,
):
    import process_control
    from process_control import ProcessIdentity, stop_owned_process

    stale = ProcessIdentity(pid=91, pgid=91, session_id=91, start_time=200)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(process_control, "_leader_identity_matches", lambda identity: False)
    monkeypatch.setattr(process_control, "_group_members", lambda identity: ((92, 201),))
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))

    result = stop_owned_process(stale, grace_seconds=0.01)

    assert result.result == "skipped_identity_mismatch"
    assert sent == []


def test_should_send_term_to_group_and_not_kill_after_graceful_exit(tmp_path: Path):
    from process_control import start_managed_process, stop_owned_process

    handle = start_managed_process(
        label="ollama",
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        env=dict(os.environ),
    )

    result = stop_owned_process(handle.identity, grace_seconds=1.0)
    handle.process.wait(timeout=5)

    assert result.result == "stopped_term"
    assert handle.process.returncode == -signal.SIGTERM


def test_should_kill_only_same_identity_when_term_grace_expires(tmp_path: Path):
    from process_control import start_managed_process, stop_owned_process

    marker = tmp_path / "ready"
    code = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, lambda *_: None); "
        f"Path({str(marker)!r}).touch(); time.sleep(30)"
    )
    handle = start_managed_process(
        label="backend",
        command=(sys.executable, "-c", code),
        cwd=tmp_path,
        env=dict(os.environ),
    )
    _wait_for_file(marker)

    result = stop_owned_process(handle.identity, grace_seconds=0.01)
    handle.process.wait(timeout=5)

    assert result.result == "stopped_kill"
    assert handle.process.returncode == -signal.SIGKILL


def test_should_report_failed_stop_when_group_survives_kill(
    monkeypatch: pytest.MonkeyPatch,
):
    import process_control
    from process_control import ProcessIdentity, stop_owned_process

    identity = ProcessIdentity(pid=91, pgid=91, session_id=91, start_time=200)
    sent: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(process_control, "_leader_identity_matches", lambda value: True)
    monkeypatch.setattr(process_control, "_group_has_live_members", lambda value: True)
    monkeypatch.setattr(process_control, "_wait_until_group_exits", lambda value, deadline: False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sent_signal: sent.append((pgid, sent_signal)))

    with pytest.raises(RuntimeError, match="process group 91 remained alive after SIGKILL"):
        stop_owned_process(identity, grace_seconds=0.01)

    assert sent == [(91, signal.SIGTERM), (91, signal.SIGKILL)]


def test_should_kill_remaining_group_child_after_leader_exits_on_term(tmp_path: Path):
    from process_control import start_managed_process, stop_owned_process

    marker = tmp_path / "child-pid"
    child_code = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, lambda *_: None); "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    leader_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(30)"
    )
    handle = start_managed_process(
        label="backend",
        command=(sys.executable, "-c", leader_code),
        cwd=tmp_path,
        env=dict(os.environ),
    )
    _wait_for_file(marker)
    child_pid = int(marker.read_text(encoding="utf-8"))

    result = stop_owned_process(handle.identity, grace_seconds=0.05)
    handle.process.wait(timeout=5)

    assert result.result == "stopped_kill"
    assert handle.process.returncode == -signal.SIGTERM
    child_stat = Path(f"/proc/{child_pid}/stat")
    assert not child_stat.exists() or child_stat.read_text().split(")", 1)[1].split()[0] == "Z"


def test_should_stop_owned_group_after_leader_exits_before_cleanup(tmp_path: Path):
    from process_control import start_managed_process, stop_managed_process

    marker = tmp_path / "child-pid"
    child_code = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, lambda *_: None); "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    leader_code = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])"
    )
    handle = start_managed_process(
        label="backend",
        command=(sys.executable, "-c", leader_code),
        cwd=tmp_path,
        env=dict(os.environ),
    )
    _wait_for_file(marker)
    child_pid = int(marker.read_text(encoding="utf-8"))
    handle.process.wait(timeout=5)

    result = stop_managed_process(handle, grace_seconds=0.05)

    assert result.result == "stopped_kill"
    child_stat = Path(f"/proc/{child_pid}/stat")
    assert not child_stat.exists() or child_stat.read_text().split(")", 1)[1].split()[0] == "Z"
