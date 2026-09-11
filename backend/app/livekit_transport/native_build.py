"""コンテナに同梱したLiveKit FFIの版と、既に稼働中のprocessでの実ロードを照合する。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from importlib.metadata import version
from pathlib import Path
from typing import Any

SDK_DIRECTORY = Path('/opt/digital-souls/livekit')
FIELDS = frozenset({
    'schema_version', 'python_sdk_version', 'ffi_version', 'source_revision',
    'patch_sha256', 'library_sha256', 'reconnect_policy', 'target', 'cuda_video_codecs_enabled',
})


def validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != FIELDS:
        raise ValueError('native_sdk_record_invalid')
    if (type(record['schema_version']) is not int or record['schema_version'] != 1
            or record['python_sdk_version'] != '1.1.16' or record['ffi_version'] != '0.12.73'
            or record['reconnect_policy'] != 'short-outage-v1'
            or record['target'] != 'x86_64-unknown-linux-gnu'
            or record['cuda_video_codecs_enabled'] is not False):
        raise ValueError('native_sdk_record_invalid')
    for field, size in [('source_revision', 40), ('patch_sha256', 64), ('library_sha256', 64)]:
        if not isinstance(record[field], str) or not re.fullmatch(f'[a-f0-9]{{{size}}}', record[field]):
            raise ValueError('native_sdk_record_invalid')
    return dict(record)


def mapped_library(library: Path, proc: Path) -> bool:
    inode = str(library.stat().st_ino)
    for process in proc.iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            mappings = (process / 'maps').read_text()
        except OSError:
            continue
        for line in mappings.splitlines():
            fields = line.split(None, 5)
            if (len(fields) == 6 and 'x' in fields[1] and fields[4] == inode
                    and fields[5] == str(library)):
                return True
    return False


def observe(directory: Path = SDK_DIRECTORY, proc: Path = Path('/proc')) -> dict[str, Any]:
    library = directory / 'liblivekit_ffi.so'
    if os.environ.get('LIVEKIT_LIB_PATH') != str(library):
        return {'status': 'missing', 'reason': 'native_sdk_override'}
    try:
        body = (directory / 'build.json').read_bytes()
        if len(body) > 4096:
            raise ValueError('native_sdk_record_invalid')
        record = validate_record(json.loads(body))
        if version('livekit') != record['python_sdk_version']:
            return {'status': 'missing', 'reason': 'native_sdk_version_mismatch'}
        if hashlib.sha256(library.read_bytes()).hexdigest() != record['library_sha256']:
            return {'status': 'missing', 'reason': 'native_sdk_hash_mismatch'}
        if not mapped_library(library, proc):
            return {'status': 'missing', 'reason': 'native_sdk_not_loaded'}
        return {'status': 'verified', 'build': record}
    except (OSError, ValueError, KeyError):
        return {'status': 'missing', 'reason': 'native_sdk_record_unavailable'}


if __name__ == '__main__':
    print(json.dumps(observe(), allow_nan=False))
