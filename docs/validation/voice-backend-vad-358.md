# #358 BE VAD基盤の局所検証

## 範囲と未完了事項

#394の部分実装。連続16kHz mono PCM16から、既存Silero legacyとlibfvadでcandidate／confirmed／ended／misfireを得る処理をBEへ実装した。
既存LiveKit経路の切替、正式utterance ID・入力世代、track欠落統計、Core／FEの新protocolは後続作業。本書をM2全体、音声会話、性能受入の完了証跡として扱わない。

## 固定音声比較

実行コマンド:

~~~sh
backend/.venv/bin/python -m pytest -q backend/tests/unit/test_voice_input_pipeline.py backend/tests/module/test_voice_input_vad_parity.py
backend/.venv/bin/python -m mypy --config-file backend/mypy.ini backend/app/voice_input
~~~

2026-09-15、59件成功。比較対象は既存speech.wav、voice-quality-v2の40音声、固定seedの雑音・電子音の計43系列。両runtimeへ同一の16kHz PCMを渡す。相槌、take-turn、文中の間の前後音声を含むが、前後音声を連結した実会話の受入はまだ行っていない。

FEの実モデル・WASM・区間検出をNodeから実行し、BEと次を比較した。

- 発話イベント種別・開始sample・検出sample、idle reset位置は完全一致。
- 0.2／0.25／0.3／0.4の各確率閾値の判定は全frameで完全一致。
- 補助有声率・集中度・平坦度は絶対／相対誤差1e-9以内。
- 主モデル確率はCPU／WASMの演算差を許容し絶対誤差0.0005以内。区間判定の許容幅ではない。

初期3系列の診断では電子音で最大約0.00024933の確率差を観測した。CPUのgraph optimizationを無効にしても変化しなかったため、標準設定を維持した。一部の電子音・雑音を発話とする結果もFEと一致する。移設に無関係な感度改善を同時に行ったとは扱わない。
検証用venvのONNX Runtimeが1.30.0だったことを確認後、固定要件の1.27.0へ揃え、59件すべてを再実行した。

## 上限と不連続

- 未完了PCMは1536 sample未満。入力chunkは1秒、確定発話は30秒上限。
- workerは待機・実行中の合計16件かつ16×1536 sampleを上限とする。
- 呼出taskのcancelでは実行中の容量を解放しない。上限超過・cancel後は旧結果を不採用とし、reset完了まで新入力を拒否する。
- sample欠落後は該当区間を破棄し、700ms以上の静音を確認するまで後続の語尾を新発話として採用しない。
- 通知はAudioInputFaultのcodeとして呼出側へ返す。ユーザーへの話し直し案内とtrack統計からの欠落判断は後続のtransport接続で検証する。
- モデルの状態分離、連続無音、長発話、欠落、不正入力、asset破損、reset競合、cancel中の容量保持をunitで確認した。

## CPU・memoryの局所観測

同じWSL2の隔離venvで、4個の入力状態を交互に実行した。1回1536 sample、固定seed 358のPCM16雑音を200 frameずつ、合計800 frame。時間はfeed呼出前後のperf_counter差、RSSはresource.getrusageのプロセス最大値。イベントループでの待機、network、STT／LLM／TTS、並列負荷は含まない。

| 項目 | 観測 |
|---|---|
| Python／ONNX Runtime／NumPy／Wasmtime | 3.12.3／1.27.0／2.5.3／36.0.0 |
| OS | Linux 6.6.87.2 microsoft WSL2、x86_64 |
| readiness実推論・WASM初期化 | 約25.98ms |
| 96ms音声frameの処理 p50／p95／最大 | 約0.657／0.764／1.371ms |
| モデル初期化前／処理後の最大RSS | 56,896／97,344 KiB |

最大RSS差は解放後の常駐量・Session単位の使用量ではない。#358の前後比較や共有推論への影響は、M5／M6の同条件測定で別に確認する。


## 2026-09-15: 既存Frontend単体試験の新契約への移行

M3／M4接続実装（`b0b0452`）に対して、既存のApp・録音UI・共通client・Room・Core／private契約テストを更新した。

- FEがspeechイベントを発行するfixtureを、BEの入力開始ACKと発話通知を受け取るfixtureへ変更した。focus、手動／スレッド切替mute、再接続、相槌／take-turn、text優先、結果照合、履歴表示の既存検証を維持した。
- 入力再開時は新しいtrack SIDをpublishし、ACKを待つ。録音UIがsuspended解除だけでtrackを有効化しないこと、publish／入力開始失敗でtrackを解放することを検証した。
- 録音UIの旧continuous専用VAD試験は、新しい責務に合わせてVAD非依存の開始・停止・再開・失敗試験に置き換えた。発話境界・短発話・quiet resetの同値性は、本書のBackend実VAD parity試験が担当する。旧WebSocket録音のVAD試験は保持した。
- CoreとLiveKit privateのfixtureは2.0に更新し、別契約の画面要求は1.0を保持した。非互換protocolを拒否する試験も保持した。

検証結果:

- `cd frontend && node node_modules/vitest/vitest.mjs run .unit.test.ts`: **873 passed、0 failed**。
- `cd frontend && npm run check`: Svelte **0 errors / 0 warnings**、E2E／integrationのTypeScript検査成功。

このUI単体試験ではRoomとBE通知をモックしている。実PCMからの境界検出、実LiveKit／STT／LLM／TTSの往復、ブラウザE2Eの実行、移設前後の品質測定、人による実マイク・聴感の受入を証明するものではない。Backendの既存試験とM5／M6は継続中。
