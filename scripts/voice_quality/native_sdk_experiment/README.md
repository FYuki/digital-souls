# LiveKit再接続の隔離SDK実験

#150の実障害22では、Python SDK 1.1.16のネイティブFFIが、接続失敗後の次回試行まで約5.13秒待っていた。
このディレクトリは、短い断の最初の10秒だけ再試行間隔を250〜500msにする検証用patchとビルド手順を保存する。
製品のSDK差し替えを有効にする設定ではない。正式な配布・更新方法と他機能への影響は別途確認する。

- 上流Rust/FFI: `63128d01d955d9d8967544f46cff64a361232bf6` / `0.12.73`
- protocol: `28e604c046c6aec29757cabed341b86458cc40f9`
- libyuv: `917276084a49be726c90292ff0a6b0a3d571a6af`
- ツールチェーン: Rust 1.97.1、Debian bookworm、Linux x86_64
- 上流ライセンスはApache-2.0。patch内の表示を保持し、配布時には上流LICENSEとWebRTCのライセンスも保持する。
- 今回のビルドはCUDA動画codecを含まない。ビルドには固定のWebRTC配布バイナリとCargo.lockの依存を取得する。
- 最初の10秒以降は従来の指数backoffを維持する。新規試行は最大64回・開始から60秒まで。
  進行中の接続試行のtimeoutはSDKの既存設定に従う。認証拒否、serverによる終了、closeの処理は変更しない。

## ビルド

Python 3.12以降、patch、Dockerを用意し、リポジトリのルートで実行する。
作業先には存在しない専用のパスを使う。prepare.pyはSDK・protocolのarchiveのSHA-256を照合し、一致しなければ停止する。
libyuvのGitiles archiveは同じcommitでも包装の時刻等が変わるため、全entryのpath・種別・mode・link先・ファイル本文ハッシュの一覧を正規化してSHA-256で照合する。
SDK、Cargo cache、成果物は専用作業先にだけ置く。

```bash
python3 scripts/voice_quality/native_sdk_experiment/prepare.py /tmp/issue150-sdk-rebuild

docker build --tag ds-issue150-rust-builder:20260908 scripts/voice_quality/native_sdk_experiment

docker run --rm --name ds-issue150-sdk-rebuild \
  --cpus 4 --memory 6g --pids-limit 512 \
  --cap-drop ALL --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  --env CARGO_HOME=/work/.cargo-cache --env RUSTUP_TOOLCHAIN=1.97.1 \
  --mount type=bind,src=/tmp/issue150-sdk-rebuild/source,dst=/work \
  --mount "type=bind,src=$(pwd)/scripts/voice_quality/native_sdk_experiment/build.sh,dst=/build.sh,readonly" \
  ds-issue150-rust-builder:20260908 sh /build.sh
```

build.shは再試行の単体検証4件と、`cargo build --locked --release --package livekit-ffi`を実行する。
上流に由来するコンパイラ警告がある。成果物のハッシュも記録するが、ビルドパス等の差により同一ハッシュになることまでは保証しない。
Dockerベースはdigest固定、OSパッケージはbookwormの取得時点の版となる。

## 実通信での読み込みと記録

実23〜25では、ビルドした`liblivekit_ffi.so`を各試行の新しいtest data rootへコピーし、
その試行のBackendにだけ`LIVEKIT_LIB_PATH`を設定した。data rootの初期化には
`initialize_runtime_data_root`を使用した。既存venvや共有サービスのライブラリは変更していない。
Docker内で同じdata rootを参照できる既存のmountを使い、Backend processのmemory mapとSHA-256で実ロードを確認した。

試験条件・版・ハッシュと結果は
[実験の記録](../../../docs/artifacts/livekit-native-sdk-experiment-2026-09-08.json)、
[匿名集計](../../../docs/artifacts/livekit-reconnect-sdk-pilot-2026-09-08.json)、
[測定の経緯](../../../docs/livekit-voice-quality-pilot-2026-09-06.md)を参照する。
3件はすべて3秒以内に復旧したが、正式な独立100件測定は未実施であり、受け入れ完了を示さない。
