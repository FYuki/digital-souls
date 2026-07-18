from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, cast


IDENTITY_ROLLBACK_GRACE_SECONDS = 1.0
PROCESS_STOP_REQUESTED = "signaled"
PROCESS_NOT_RUNNING = "not_running"
PROCESS_IDENTITY_MISMATCH = "skipped_identity_mismatch"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    pgid: int
    session_id: int
    start_time: int

    def to_report(self) -> dict[str, int]:
        return {
            "pid": self.pid,
            "pgid": self.pgid,
            "sessionId": self.session_id,
            "startTime": self.start_time,
        }

    @classmethod
    def from_report(cls, value: Mapping[str, object]) -> ProcessIdentity:
        fields = ("pid", "pgid", "sessionId", "startTime")
        if any(isinstance(value.get(field), bool) or not isinstance(value.get(field), int) for field in fields):
            raise ValueError("invalid process identity")
        return cls(
            pid=cast(int, value["pid"]),
            pgid=cast(int, value["pgid"]),
            session_id=cast(int, value["sessionId"]),
            start_time=cast(int, value["startTime"]),
        )


@dataclass(frozen=True)
class ManagedProcess:
    label: str
    process: subprocess.Popen[bytes]
    identity: ProcessIdentity


@dataclass(frozen=True)
class ProcessStopResult:
    result: str


def _process_stat(pid: int) -> tuple[str, int, int, int]:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields_after_name = stat[stat.rfind(")") + 2 :].split()
    return (
        fields_after_name[0],
        int(fields_after_name[2]),
        int(fields_after_name[3]),
        int(fields_after_name[19]),
    )


def _read_identity(pid: int) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        pgid=os.getpgid(pid),
        session_id=os.getsid(pid),
        start_time=_process_stat(pid)[3],
    )


def current_process_identity() -> ProcessIdentity:
    return _read_identity(os.getpid())


def request_process_stop(identity: ProcessIdentity) -> ProcessStopResult:
    try:
        pidfd = os.pidfd_open(identity.pid)
    except ProcessLookupError:
        return ProcessStopResult(PROCESS_NOT_RUNNING)
    try:
        try:
            state = _process_stat(identity.pid)[0]
            current_identity = _read_identity(identity.pid)
        except (FileNotFoundError, ProcessLookupError):
            return ProcessStopResult(PROCESS_NOT_RUNNING)
        if state == "Z":
            return ProcessStopResult(PROCESS_NOT_RUNNING)
        if current_identity != identity:
            return ProcessStopResult(PROCESS_IDENTITY_MISMATCH)
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        except ProcessLookupError:
            return ProcessStopResult(PROCESS_NOT_RUNNING)
        return ProcessStopResult(PROCESS_STOP_REQUESTED)
    finally:
        os.close(pidfd)


def start_managed_process(
    *, label: str, command: tuple[str, ...], cwd: Path, env: Mapping[str, str]
) -> ManagedProcess:
    if not command:
        raise ValueError("managed process command must not be empty")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        identity = _read_identity(process.pid)
    except (FileNotFoundError, ProcessLookupError, OSError, ValueError) as error:
        _rollback_unidentified_process(process)
        raise RuntimeError(f"{label} exited before its identity could be recorded") from error
    return ManagedProcess(label=label, process=process, identity=identity)


def _rollback_unidentified_process(process: subprocess.Popen[bytes]) -> None:
    pgid = process.pid
    _signal_new_process_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + IDENTITY_ROLLBACK_GRACE_SECONDS
    if _wait_until_process_group_exits(pgid, deadline):
        process.wait(timeout=IDENTITY_ROLLBACK_GRACE_SECONDS)
        return
    _signal_new_process_group(pgid, signal.SIGKILL)
    group_exited = _wait_until_process_group_exits(
        pgid, time.monotonic() + IDENTITY_ROLLBACK_GRACE_SECONDS
    )
    process.wait(timeout=IDENTITY_ROLLBACK_GRACE_SECONDS)
    if not group_exited:
        raise RuntimeError(f"process group {pgid} remained alive after SIGKILL")


def _signal_new_process_group(pgid: int, sent_signal: signal.Signals) -> None:
    try:
        os.killpg(pgid, sent_signal)
    except ProcessLookupError:
        return


def _wait_until_process_group_exits(pgid: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if not _process_group_has_live_members(pgid):
            return True
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    return not _process_group_has_live_members(pgid)


def _process_group_has_live_members(pgid: int) -> bool:
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            state, member_pgid, _session_id, _start_time = _process_stat(
                int(process_dir.name)
            )
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError):
            continue
        if state != "Z" and member_pgid == pgid:
            return True
    return False


def process_identity_matches(identity: ProcessIdentity) -> bool:
    try:
        state, _pgid, _session_id, _start_time = _process_stat(identity.pid)
        return state != "Z" and _read_identity(identity.pid) == identity
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError):
        return False


def _leader_identity_matches(identity: ProcessIdentity) -> bool:
    try:
        return _read_identity(identity.pid) == identity
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError):
        return False


def _group_members(identity: ProcessIdentity) -> tuple[tuple[int, int], ...]:
    members: list[tuple[int, int]] = []
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        try:
            state, pgid, session_id, start_time = _process_stat(pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError):
            continue
        if state != "Z" and pgid == identity.pgid and session_id == identity.session_id:
            members.append((pid, start_time))
    return tuple(members)


def _managed_group_identity_matches(process: ManagedProcess) -> bool:
    identity = process.identity
    if process.process.pid != identity.pid:
        return False
    members = _group_members(identity)
    if any(start_time < identity.start_time for _pid, start_time in members):
        return False
    if _leader_identity_matches(identity):
        return True
    return process.process.poll() is not None and bool(members)


def _group_has_live_members(identity: ProcessIdentity) -> bool:
    return bool(_group_members(identity))


def _wait_until_group_exits(identity: ProcessIdentity, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if not _group_has_live_members(identity):
            return True
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    return not _group_has_live_members(identity)


def _stop_process_group(
    identity: ProcessIdentity, *, grace_seconds: float
) -> ProcessStopResult:
    owned_members = frozenset(_group_members(identity))
    if not owned_members:
        return ProcessStopResult("stopped_term")
    try:
        os.killpg(identity.pgid, signal.SIGTERM)
    except ProcessLookupError:
        if not _group_has_live_members(identity):
            return ProcessStopResult("stopped_term")
        raise
    if _wait_until_group_exits(identity, time.monotonic() + grace_seconds):
        return ProcessStopResult("stopped_term")
    current_members = frozenset(_group_members(identity))
    if not owned_members.intersection(current_members):
        return ProcessStopResult(PROCESS_IDENTITY_MISMATCH)
    try:
        os.killpg(identity.pgid, signal.SIGKILL)
    except ProcessLookupError:
        if not _group_has_live_members(identity):
            return ProcessStopResult("stopped_term")
        raise
    if not _wait_until_group_exits(
        identity, time.monotonic() + max(1.0, grace_seconds)
    ):
        raise RuntimeError(
            f"process group {identity.pgid} remained alive after SIGKILL"
        )
    return ProcessStopResult("stopped_kill")


def _validate_grace_seconds(grace_seconds: float) -> None:
    if (
        isinstance(grace_seconds, bool)
        or not isinstance(grace_seconds, (int, float))
        or grace_seconds < 0
    ):
        raise ValueError("grace_seconds must be a non-negative number")


def stop_owned_process(
    identity: ProcessIdentity, *, grace_seconds: float
) -> ProcessStopResult:
    _validate_grace_seconds(grace_seconds)
    if not _leader_identity_matches(identity):
        return ProcessStopResult(PROCESS_IDENTITY_MISMATCH)
    return _stop_process_group(identity, grace_seconds=grace_seconds)


def stop_managed_process(
    process: ManagedProcess, *, grace_seconds: float
) -> ProcessStopResult:
    _validate_grace_seconds(grace_seconds)
    if not _managed_group_identity_matches(process):
        return ProcessStopResult(PROCESS_IDENTITY_MISMATCH)
    return _stop_process_group(process.identity, grace_seconds=grace_seconds)
