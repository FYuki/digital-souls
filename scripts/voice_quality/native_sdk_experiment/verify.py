"""コンテナビルド専用の固定LiveKit FFI検証。実通信・通常終了の受入とは分ける。"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path


def verify(record_path: Path) -> None:
    record = json.loads(record_path.read_text())
    if version("livekit") != record["python_sdk_version"]:
        raise RuntimeError("Python SDKと固定FFIの対応版が一致しません")
    library = Path(os.environ["LIVEKIT_LIB_PATH"])
    if hashlib.sha256(library.read_bytes()).hexdigest() != record["library_sha256"]:
        raise RuntimeError("固定FFIのSHA-256がビルド記録と一致しません")

    from livekit.rtc._ffi_client import FfiClient

    client = FfiClient.instance
    if client is None:
        raise RuntimeError("固定FFIを初期化できませんでした")
    client._ffi_lib.livekit_ffi_dispose()


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
    print("固定LiveKit FFIの版・ハッシュ・初期化・明示解放を確認しました", flush=True)
    sys.stderr.flush()
    # 固定SDKのdisposeはログ転送taskの終了を待たない。
    # 検証成功後だけ、Python finalizationやatexitの再disposeを経ずに終了する。
    # 初期化・解放中の異常や検証例外はここへ到達せず、ビルドを失敗させる。
    # この一時processだけの境界であり、Backendの終了処理には使用しない。
    os._exit(0)
