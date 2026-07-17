from __future__ import annotations

import signal
from contextlib import contextmanager
from collections.abc import Iterator
from types import FrameType
from typing import Callable, Mapping, cast


SignalHandler = Callable[[int, FrameType | None], object]
InstalledHandler = signal.Handlers | SignalHandler | None
INTERRUPT_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM)


def install_interrupt_handlers() -> tuple[
    Callable[[], bool], dict[signal.Signals, InstalledHandler]
]:
    interrupted = False

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        nonlocal interrupted
        interrupted = True
        raise InterruptedError("environment run interrupted")

    previous = {
        signal_number: cast(InstalledHandler, signal.signal(signal_number, interrupt))
        for signal_number in INTERRUPT_SIGNALS
    }
    return lambda: interrupted, previous


def restore_interrupt_handlers(
    previous: Mapping[signal.Signals, InstalledHandler]
) -> None:
    for signal_number, handler in previous.items():
        signal.signal(signal_number, handler)


@contextmanager
def block_interrupt_signals() -> Iterator[None]:
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, INTERRUPT_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


@contextmanager
def defer_interrupt_signals() -> Iterator[None]:
    pending: list[int] = []

    def defer(signum: int, _frame: FrameType | None) -> None:
        pending.append(signum)

    previous = {
        signum: cast(InstalledHandler, signal.signal(signum, defer))
        for signum in INTERRUPT_SIGNALS
    }
    try:
        yield
    finally:
        restore_interrupt_handlers(previous)
        if pending:
            pending_signum = pending[0]
            handler = previous[signal.Signals(pending_signum)]
            if callable(handler):
                handler(pending_signum, None)
            elif handler != signal.SIG_IGN:
                raise InterruptedError("environment run interrupted")


@contextmanager
def coalesce_interrupt_signals() -> Iterator[None]:
    previous = {
        signum: cast(InstalledHandler, signal.signal(signum, signal.SIG_IGN))
        for signum in INTERRUPT_SIGNALS
    }
    try:
        yield
    finally:
        restore_interrupt_handlers(previous)
