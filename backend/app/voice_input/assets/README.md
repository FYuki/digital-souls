# BE音声入力の固定資産

#358でFEの既存資産を変更せず同梱する。音声や会話本文の資産ではない。

| 資産 | 配布元・照合 | ライセンス |
|---|---|---|
| silero_vad_legacy.onnx | @ricky0123/vad-web 0.0.30 の dist、1,807,522 bytes、SHA256 a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28 | [Silero MIT](LICENSE.silero) |
| libfvad.wasm | [既存FE資産](../../../../frontend/src/lib/audio/vendor/README.md)、15,331 bytes、SHA256 3fadafc9c5c1c3117d0178ae631484c6dabc72e0f8742b95c370cd39f46cf171 | [libfvad/WebRTC BSD](LICENSE.libfvad) |

BackendのimageへCOPYされ、runtimeのモデル取得は行わない。起動時にhash・I/O・CPU推論・補助WASMを確認する。
主モデルのh/c、WASMのstore/memory、検出器、音声bufferはSessionごとに分離する。
[移設契約](../../../../docs/voice-backend-migration-contract.md)の値と同じframeに対するFE/BE比較を維持する。
