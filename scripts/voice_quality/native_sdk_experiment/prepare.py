"""#150のSDK隔離実験用に、固定した公開ソースとpatchを新しい作業先へ準備する。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from urllib.request import urlopen

REVISION = "63128d01d955d9d8967544f46cff64a361232bf6"
SOURCES = (
    (
        "sdk",
        f"https://codeload.github.com/livekit/rust-sdks/tar.gz/{REVISION}",
        "bfc6b4539137a84420c8c8305f2c43d125f83f9de2912283bbba682106969266",
        "",
        True,
    ),
    (
        "protocol",
        "https://codeload.github.com/livekit/protocol/tar.gz/28e604c046c6aec29757cabed341b86458cc40f9",
        "2ae511c80f8eb4a1d18e0028ac518a2bca5afd68af2600a3b93b093b3a32bef7",
        "livekit-protocol/protocol",
        True,
    ),
    (
        "libyuv",
        "https://chromium.googlesource.com/libyuv/libyuv/+archive/917276084a49be726c90292ff0a6b0a3d571a6af.tar.gz",
        "279e6c72880a53616a5ece2d81ff4f86490500b0bb7099ce804648136b25f36d",
        "yuv-sys/libyuv",
        False,
    ),
)


def source_digest(name: str, archive: bytes) -> str:
    if name != "libyuv":
        return hashlib.sha256(archive).hexdigest()
    # Gitilesは同じcommitでもarchiveの時刻等が変わるため、全entryの内容と属性を照合する。
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 10000 or sum(member.size for member in members) > 100_000_000:
            raise ValueError("libyuv archiveの展開量が上限を超えました")
        rows = []
        for member in members:
            content = tar.extractfile(member) if member.isfile() else None
            digest = hashlib.sha256(content.read()).hexdigest() if content else None
            rows.append((member.name, member.type.decode("ascii"), member.mode, member.linkname, digest))
    canonical = json.dumps(sorted(rows), separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def prepare(destination: Path) -> None:
    if not destination.is_absolute():
        raise ValueError("作業先は新しい絶対パスで指定してください")
    destination.mkdir(parents=True, exist_ok=False)
    source = destination / "source"
    source.mkdir()
    for name, url, expected, relative, strip_root in SOURCES:
        with urlopen(url, timeout=60) as response:
            archive = response.read(30_000_001)
        if len(archive) > 30_000_000 or source_digest(name, archive) != expected:
            raise ValueError(f"公開archiveの検証に失敗しました: {name}")
        (destination / f"{name}.tar.gz").write_bytes(archive)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                if strip_root:
                    member.name = "/".join(member.name.split("/")[1:])
                    if not member.name:
                        continue
                tar.extract(member, source / relative, filter="data")
    patch = Path(__file__).with_name("short-outage-retry.patch")
    subprocess.run(
        ["patch", "--batch", "--fuzz=0", "--strip=1", f"--input={patch.resolve()}"],
        cwd=source, check=True,
    )
    (source / "retry_policy_test.rs").write_text(
        '#[path = "livekit/src/rtc_engine/reconnect_strategy.rs"]\nmod policy;\n'
    )
    print("固定ソースと検証用patchを準備しました")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    prepare(parser.parse_args().destination)
