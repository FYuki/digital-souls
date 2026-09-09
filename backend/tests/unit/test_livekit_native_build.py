from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from app.livekit_transport import native_build


@pytest.fixture
def native(tmp_path: Path, monkeypatch):
    library = tmp_path / 'liblivekit_ffi.so'
    library.write_bytes(b'library fixture')
    record = dict(schema_version=1, python_sdk_version='1.1.16', ffi_version='0.12.73',
                  source_revision='a'*40, patch_sha256='b'*64,
                  library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                  reconnect_policy='short-outage-v1', target='x86_64-unknown-linux-gnu',
                  cuda_video_codecs_enabled=False)
    (tmp_path / 'build.json').write_text(json.dumps(record))
    proc = tmp_path / 'proc'
    pid = proc / str(os.getpid()+1000)
    pid.mkdir(parents=True)
    (pid / 'maps').write_text(f'1000-2000 r-xp 0000 00:00 {library.stat().st_ino} {library}\n')
    monkeypatch.setenv('LIVEKIT_LIB_PATH', str(library))
    monkeypatch.setattr(native_build, 'version', lambda _: '1.1.16')
    return tmp_path, library, proc, record


def test_actual_executable_mapping_and_hash_are_required(native):
    root, library, proc, record = native
    assert native_build.observe(root, proc) == {'status': 'verified', 'build': record}
    next(proc.iterdir()).joinpath('maps').write_text('')
    assert native_build.observe(root, proc)['reason'] == 'native_sdk_not_loaded'
    library.write_bytes(b'tampered')
    assert native_build.observe(root, proc)['reason'] == 'native_sdk_hash_mismatch'


def test_override_or_wrong_version_cannot_claim_candidate(native, monkeypatch):
    root, library, proc, _ = native
    monkeypatch.setenv('LIVEKIT_LIB_PATH', '/other/sdk.so')
    assert native_build.observe(root, proc)['reason'] == 'native_sdk_override'
    monkeypatch.setenv('LIVEKIT_LIB_PATH', str(library))
    monkeypatch.setattr(native_build, 'version', lambda _: '1.1.17')
    assert native_build.observe(root, proc)['reason'] == 'native_sdk_version_mismatch'


@pytest.mark.parametrize('field,value', [('schema_version', True), ('source_revision', 'private-value'),
                                        ('patch_sha256', 'bad'), ('extra', 'private-value')])
def test_arbitrary_metadata_is_rejected_without_echo(native, field, value):
    root, _, proc, record = native
    record[field] = value
    (root / 'build.json').write_text(json.dumps(record))
    result = native_build.observe(root, proc)
    assert result['status'] == 'missing'
    assert 'private-value' not in json.dumps(result)


def test_readonly_or_deleted_mapping_does_not_prove_loaded(native):
    root, library, proc, _ = native
    maps = next(proc.iterdir()) / 'maps'
    maps.write_text(f'1000-2000 r--p 0000 00:00 {library.stat().st_ino} {library}\n')
    assert native_build.observe(root, proc)['reason'] == 'native_sdk_not_loaded'
    maps.write_text(f'1000-2000 r-xp 0000 00:00 {library.stat().st_ino} {library} (deleted)\n')
    assert native_build.observe(root, proc)['reason'] == 'native_sdk_not_loaded'
