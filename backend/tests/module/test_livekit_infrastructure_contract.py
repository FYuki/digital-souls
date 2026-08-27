from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def _load_yaml(path: Path) -> dict[str, object]:
    assert path.is_file()
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_dev_livekit_uses_host_network_and_the_fixed_single_udp_mux() -> None:
    compose = _load_yaml(ROOT / "infra" / "livekit" / "compose.yaml")
    config = _load_yaml(ROOT / "infra" / "livekit" / "livekit.yaml")
    service = compose["services"]["livekit"]

    assert service["network_mode"] == "host"
    assert "ports" not in service
    assert service["environment"] == {
        "LIVEKIT_KEYS": "${LIVEKIT_KEYS:?LIVEKIT_KEYS is required}"
    }
    assert config["port"] == 7880
    assert config["bind_addresses"] == ["127.0.0.1"]
    assert "keys" not in config
    assert config["rtc"] == {
        "tcp_port": 7881,
        "udp_port": 7882,
        "use_external_ip": False,
    }
    assert set(config).isdisjoint({"turn", "redis", "tls"})


def test_dogfood_livekit_template_uses_reserved_ports_without_extra_services() -> None:
    config = _load_yaml(
        ROOT / "infra" / "dogfood" / "templates" / "livekit.yaml"
    )

    assert config["port"] == 17880
    assert config["rtc"] == {
        "tcp_port": 17881,
        "udp_port": 17882,
        "use_external_ip": False,
    }
    assert set(config).isdisjoint({"turn", "redis", "tls"})
