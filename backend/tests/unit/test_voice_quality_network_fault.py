from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

import pytest

_PATH = Path(__file__).resolve().parents[3] / 'scripts/voice_quality/network_fault.py'
_SPEC = importlib.util.spec_from_file_location('voice_quality_network_fault', _PATH)
assert _SPEC is not None and _SPEC.loader is not None
fault = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fault
_SPEC.loader.exec_module(fault)


def records():
    container = {
        'Id': 'test-container', 'State': {'Running': True},
        'Config': {'Labels': {
            fault.PURPOSE_LABEL: fault.PURPOSE,
            'io.digital-souls.environment': 'test',
        }},
        'HostConfig': {'NetworkMode': 'test-bridge'},
        'NetworkSettings': {'Networks': {'test-bridge': {
            'NetworkID': 'test-network', 'IPAddress': '172.20.0.2',
        }}},
    }
    network = {
        'Id': 'test-network', 'Driver': 'bridge',
        'Labels': {fault.PURPOSE_LABEL: fault.PURPOSE},
        'Containers': {'test-container': {}},
    }
    return container, network


@pytest.mark.parametrize('unsafe', [
    'unlabelled', 'dogfood', 'host', 'not-running', 'shared-network',
    'unlabelled-network', 'multiple-networks', 'wrong-network',
])
def test_fault_refuses_non_dedicated_targets(unsafe):
    container, network = records()
    if unsafe == 'unlabelled':
        container['Config']['Labels'] = {}
    elif unsafe == 'dogfood':
        container['Config']['Labels']['io.digital-souls.environment'] = 'dogfood'
    elif unsafe == 'host':
        container['HostConfig']['NetworkMode'] = 'host'
    elif unsafe == 'not-running':
        container['State']['Running'] = False
    elif unsafe == 'shared-network':
        network['Containers']['unrelated-container'] = {}
    elif unsafe == 'unlabelled-network':
        network['Labels'] = None
    elif unsafe == 'multiple-networks':
        container['NetworkSettings']['Networks']['other'] = {}
    elif unsafe == 'wrong-network':
        network['Id'] = 'other-network'
    with pytest.raises(ValueError):
        fault.validate_target(container, network)


def test_fault_restores_original_ip_when_wait_is_interrupted(monkeypatch):
    container, network = records()
    target = fault.validate_target(container, network)
    disconnected = deepcopy(container)
    disconnected['NetworkSettings']['Networks'] = {}
    operations = []
    events = []

    def docker(*arguments):
        operations.append(arguments)
        return [disconnected] if arguments[0] == 'inspect' else None

    def interrupted(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(fault, 'docker', docker)
    monkeypatch.setattr(fault, 'emit', events.append)
    monkeypatch.setattr(fault.time, 'sleep', interrupted)
    with pytest.raises(KeyboardInterrupt):
        fault.pulse(target, 2)
    assert operations[-1] == (
        'network', 'connect', '--ip', '172.20.0.2', 'test-network', 'test-container',
    )
    assert events == ['network_link_disconnected', 'network_link_restored']


def test_fault_labels_tcp_probe_separately_from_media_recovery(monkeypatch):
    container, network = records()
    target = fault.validate_target(container, network)
    disconnected = deepcopy(container)
    disconnected['NetworkSettings']['Networks'] = {}
    events = []
    monkeypatch.setattr(fault, 'docker', lambda *args: [disconnected] if args[0] == 'inspect' else None)
    monkeypatch.setattr(fault, 'emit', events.append)
    monkeypatch.setattr(fault.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(fault.socket, 'create_connection', lambda *args, **kwargs: nullcontext())
    fault.pulse(target, 0.1)
    assert events == [
        'network_link_disconnected', 'network_link_restored', 'signaling_tcp_reachable',
    ]
    assert 'transport_available' not in events


@pytest.mark.parametrize('duration', [0, -1, 31, float('nan'), float('inf')])
def test_invalid_duration_never_touches_docker(monkeypatch, duration):
    def forbidden(*args):
        pytest.fail('invalid duration must not touch Docker')
    monkeypatch.setattr(fault, 'docker', forbidden)
    with pytest.raises(ValueError):
        fault.pulse(fault.Target('c', 'n', '172.20.0.2'), duration)


@pytest.mark.parametrize('change', [None, 'host_ip', 'host_port', 'extra_binding', 'missing_media'])
def test_fault_resolver_requires_the_same_dedicated_probe_endpoint(monkeypatch, change):
    container, network = records()
    container['HostConfig']['PortBindings'] = {
        f'{port}/{protocol}': [{'HostIp': '127.0.0.1', 'HostPort': str(port)}]
        for port, protocol in ((19880, 'tcp'), (19881, 'tcp'), (19882, 'udp'))
    }
    ports = container['HostConfig']['PortBindings']
    if change == 'host_ip': ports['19880/tcp'][0]['HostIp'] = '0.0.0.0'
    if change == 'host_port': ports['19880/tcp'][0]['HostPort'] = '17880'
    if change == 'extra_binding': ports['19880/tcp'].append({'HostIp': '::1', 'HostPort': '19880'})
    if change == 'missing_media': del ports['19882/udp']
    monkeypatch.setattr(fault, 'docker', lambda *args: [container if args[0] == 'inspect' else network])
    if change is None:
        assert fault.resolve_target('test-only') == fault.Target('test-container', 'test-network', '172.20.0.2')
    else:
        with pytest.raises(ValueError, match='loopback ports'):
            fault.resolve_target('test-only')
