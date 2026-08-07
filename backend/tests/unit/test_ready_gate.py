from __future__ import annotations

import pytest


@pytest.mark.parametrize("host", ["::1", "::0000:0.0.0.1"])
def test_should_open_and_close_ready_gate_on_ipv6_loopback(host: str) -> None:
    from ready_gate import ReadyGate

    ready_gate = ReadyGate({"host": host, "port": 0})

    ready_gate.open()
    ready_gate.close()
