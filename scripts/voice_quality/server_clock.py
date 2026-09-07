"""同期済み時計や一定offsetを仮定せず、probeの因果順序でサーバー状態遷移を囲む。"""
from __future__ import annotations

import math


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9007199254740991


def _client_time(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def bound_transition(probes: list[dict[str, object]], lower_ns: int, upper_ns: int,
                     generation: int) -> dict[str, object]:
    """Csend <= Sreceive <= Ssend <= transition、逆順の後続probeからC側の上下限を得る。

    server usは切捨てを外側1usへ広げ、client performanceの丸めは外側0.2msへ含める。
    midpoint、片道=RTT/2、時計速度・一定offsetを仮定しない。不足は明示欠測にする。
    """
    if (not isinstance(lower_ns, int) or isinstance(lower_ns, bool) or lower_ns < 0
            or not isinstance(upper_ns, int) or isinstance(upper_ns, bool) or upper_ns < lower_ns
            or not _integer(generation)):
        raise ValueError('invalid server transition bounds')
    before: list[float] = []
    after: list[float] = []
    missing = 0
    observed = 0
    for probe in probes:
        if probe.get('status') != 'received' or probe.get('generation') != generation:
            missing += 1
            continue
        sent, received = probe.get('sentAtMs'), probe.get('receivedAtMs')
        server_received, server_sent = probe.get('serverReceivedAtUs'), probe.get('serverSentAtUs')
        if server_received is None and server_sent is None:
            missing += 1
            continue
        if (not _client_time(sent) or not _client_time(received) or received < sent
                or not _integer(server_received) or not _integer(server_sent) or server_sent < server_received):
            raise ValueError('invalid clock probe observation')
        observed += 1
        if (server_sent + 1) * 1000 <= lower_ns:
            before.append(max(0, sent - 0.2))
        if server_received * 1000 >= upper_ns:
            after.append(received + 0.2)
    lower, upper = max(before, default=None), min(after, default=None)
    if lower is not None and upper is not None and upper < lower:
        raise ValueError('contradictory causal clock bounds')
    return {'method': 'causal_probe_brackets_v1', 'observed_probes': observed, 'missing_probes': missing,
            'before_probes': len(before), 'after_probes': len(after),
            'lower_ms': lower, 'upper_ms': upper,
            'width_ms': upper - lower if lower is not None and upper is not None else None,
            'missing_reason': None if before and after else 'server_transition_not_bracketed'}
