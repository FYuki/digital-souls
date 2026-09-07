"""ビルド済みLiveKit FFI、ライセンス、版とハッシュをコンテナ用の固定配置へ保存する。"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import tomllib


def package(source: Path, patch: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    library = source / 'target/release/liblivekit_ffi.so'
    shutil.copyfile(library, destination / library.name)
    licenses = destination / 'licenses'
    licenses.mkdir()
    for relative, name in [
        ('LICENSE', 'livekit-LICENSE'),
        ('livekit-ffi/WEBRTC_LICENSE.md', 'WebRTC-LICENSE.md'),
        ('yuv-sys/libyuv/LICENSE', 'libyuv-LICENSE'),
        ('livekit-protocol/protocol/LICENSE', 'protocol-LICENSE'),
    ]:
        shutil.copyfile(source / relative, licenses / name)
    ffi = tomllib.loads((source / 'livekit-ffi/Cargo.toml').read_text())['package']['version']
    if ffi != '0.12.73':
        raise ValueError('FFI版とPython SDKの対応を再確認してください')
    record = {
        'schema_version': 1,
        'python_sdk_version': '1.1.16',
        'ffi_version': ffi,
        'source_revision': '63128d01d955d9d8967544f46cff64a361232bf6',
        'patch_sha256': hashlib.sha256(patch.read_bytes()).hexdigest(),
        'library_sha256': hashlib.sha256(library.read_bytes()).hexdigest(),
        'reconnect_policy': 'short-outage-v1',
        'target': 'x86_64-unknown-linux-gnu',
        'cuda_video_codecs_enabled': False,
    }
    (destination / 'build.json').write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    package(*(Path(argument) for argument in sys.argv[1:]))
