from __future__ import annotations

import importlib

import pytest


def _observation_module(contract: str):
    module_name = "app.livekit_transport.observation"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is None or not (
            error.name == module_name or module_name.startswith(f"{error.name}.")
        ):
            raise
    pytest.fail(f"{module_name} must implement {contract}")


def test_server_publish_latency_uses_one_server_clock_domain() -> None:
    module = _observation_module("elapsed time within one server clock domain")
    publish_started = module.Observation(
        name="audio_publish_started",
        value=1_000,
        clock_domain="server_monotonic",
        unit="millisecond",
    )
    first_audio_out = module.Observation(
        name="first_audio_out",
        value=1_025,
        clock_domain="server_monotonic",
        unit="millisecond",
    )

    assert module.elapsed(first_audio_out, publish_started) == module.Duration(
        value=25,
        clock_domain="server_monotonic",
        unit="millisecond",
    )


def test_observation_rejects_subtraction_across_clock_domains() -> None:
    module = _observation_module("rejection of cross-clock subtraction")
    server = module.Observation(
        name="first_audio_out",
        value=2_000,
        clock_domain="server_monotonic",
        unit="millisecond",
    )
    client = module.Observation(
        name="playback_started",
        value=2_010,
        clock_domain="client_audio_context",
        unit="millisecond",
    )

    with pytest.raises(ValueError):
        module.elapsed(client, server)


def test_reconnect_is_complete_only_after_control_and_audio_are_available() -> None:
    module = _observation_module("control-and-audio reconnect readiness")
    readiness = module.ReconnectReadiness()

    readiness.control_available(at=1_000)
    assert readiness.completed_at is None

    readiness.audio_available(at=1_025)
    assert readiness.completed_at == 1_025
