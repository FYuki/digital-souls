"""専用bridgeの切断・復旧を行う。hostやdogfoodのnetwork設定は対象にしない。"""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Any

PURPOSE = "voice-quality-fault-injection"
PURPOSE_LABEL = "io.digital-souls.purpose"


def docker(*arguments: str) -> Any:
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        # inspectには環境変数も含まれる。stdout/stderrを例外へ転記しない。
        raise RuntimeError("dedicated network operation failed")
    if arguments[0] in ("inspect", "network") and result.stdout.lstrip().startswith("["):
        return json.loads(result.stdout)
    return None


@dataclass(frozen=True)
class Target:
    container_id: str
    network_id: str
    address: str


def validate_target(container: dict[str, Any], network: dict[str, Any]) -> Target:
    labels = container.get("Config", {}).get("Labels") or {}
    if labels.get(PURPOSE_LABEL) != PURPOSE or labels.get("io.digital-souls.environment") != "test":
        raise ValueError("only a labelled voice-quality test container is allowed")
    if not container.get("State", {}).get("Running"):
        raise ValueError("test container must be running")
    if container.get("HostConfig", {}).get("NetworkMode") in ("host", "none"):
        raise ValueError("a dedicated bridge is required")
    if network.get("Driver") != "bridge" or (network.get("Labels") or {}).get(PURPOSE_LABEL) != PURPOSE:
        raise ValueError("only a labelled voice-quality bridge is allowed")
    container_id, network_id = container["Id"], network["Id"]
    if set(network.get("Containers") or {}) != {container_id}:
        raise ValueError("the bridge must contain only the selected test container")
    attached = container.get("NetworkSettings", {}).get("Networks") or {}
    if len(attached) != 1:
        raise ValueError("test container must have exactly one network")
    binding = next(iter(attached.values()))
    if binding.get("NetworkID") != network_id:
        raise ValueError("selected bridge does not match the container")
    address = str(ipaddress.IPv4Address(binding["IPAddress"]))
    return Target(container_id, network_id, address)


def resolve_target(container_name: str) -> Target:
    """probe接続先と切断対象が同じ専用loopback serviceであることを確認する。"""
    container = docker("inspect", container_name)[0]
    networks = container.get("NetworkSettings", {}).get("Networks") or {}
    if len(networks) != 1:
        raise ValueError("test container must have exactly one network")
    network = docker("network", "inspect", next(iter(networks.values()))["NetworkID"])[0]
    target = validate_target(container, network)
    ports = container.get("HostConfig", {}).get("PortBindings") or {}
    for port, protocol in ((19880, "tcp"), (19881, "tcp"), (19882, "udp")):
        if ports.get(f"{port}/{protocol}") != [{"HostIp": "127.0.0.1", "HostPort": str(port)}]:
            raise ValueError("fault service must publish its dedicated loopback ports")
    return target


def emit(name: str) -> None:
    print(json.dumps({
        "event": name, "timestamp_ns": time.monotonic_ns(),
        "clock_domain": "fault_runner_monotonic",
    }), flush=True)


def pulse(target: Target, duration_seconds: float) -> None:
    if not math.isfinite(duration_seconds) or not 0.05 <= duration_seconds <= 30:
        raise ValueError("fault duration must be between 0.05 and 30 seconds")
    # 不確定なdisconnect失敗もfinallyで接続状態を確認して復旧する。
    try:
        docker("network", "disconnect", target.network_id, target.container_id)
        emit("network_link_disconnected")
        time.sleep(duration_seconds)
    finally:
        current = docker("inspect", target.container_id)[0]
        attached = current.get("NetworkSettings", {}).get("Networks") or {}
        if not any(item.get("NetworkID") == target.network_id for item in attached.values()):
            docker("network", "connect", "--ip", target.address, target.network_id, target.container_id)
        emit("network_link_restored")
    # link復旧と実疎通を区別する。これはHTTP/TCP疎通だけで、media復旧ではない。
    deadline = time.monotonic() + 10
    while True:
        try:
            with socket.create_connection(("127.0.0.1", 19880), timeout=0.2):
                emit("signaling_tcp_reachable")
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError("signaling TCP did not recover within ten seconds") from None
            time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--duration-ms", type=float, default=2_000)
    arguments = parser.parse_args()
    pulse(resolve_target(arguments.container), arguments.duration_ms / 1000)


if __name__ == "__main__":
    main()
