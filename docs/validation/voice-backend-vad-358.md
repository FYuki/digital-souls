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


## 2026-09-15: 既存Backend契約試験の移行

- Core／private protocolのfixtureを2.0へ更新した。配送の重複排除・ACK・再送・終端・再接続・認証の試験を維持した。
- old participant SIDの配送拒否試験では、clientから送れる`session_resumed`を使用する。BE専用の`session_started`をclient入力として許可する変更は行っていない。
- bootstrapにtransport versionを追加し、非対応・未指定transport、および旧Core 1.1のclientを、Room／session／runtime／token作成前に拒否する試験を追加した。
- 音声出力の単体試験はSDK package全体の差し替えをやめ、AudioSource／track出力APIだけを置き換える。AudioFrameとFrameProcessorは実SDK型を使用する。

検証結果:

- `test_livekit_private_contract.py`、`test_livekit_delivery_and_lifecycle.py`、`test_livekit_audio_probe.py`、`test_voice_session_contract.py`、`test_livekit_bootstrap_api.py`: **145 passed、0 failed**。
- `test_livekit_playback_summary.py`、`test_livekit_response_audio.py`: **31 passed、0 failed**。
- その他の既存LiveKit単体試験をファイルごとに棚卸しした。入力抑止・capture lineage・preview・runtimeの計43件は、旧FE発話イベント等に依存するため未移行。これらの失敗を合格・skipへ置き換えず、BE境界・実SDK readerに対応する検証へ更新する必要がある。
- 最初の一括実行は、無効な旧protocolの送信でcleanup待機へ入り進行しなかったため中断した。protocol fixture更新後、該当する配送suiteは上記145件の一部として正常完了した。

Backend全体の合格、実サービスの往復、M5／M6の受入完了は引き続き未確認。

## 2026-09-15: BE終了境界の固定と既存Backend試験の更新

終了したcaptureへ、欠落確認を待っている間に後続PCMが追記される経路を修正した。
終了済み区間を固定し、次のPCMは未終了captureまたは上限付きprerollだけへ渡す。
旧FE通知後のmedia tail timerを削除し、BE境界と発話ごとの欠落確認成功をSTT開始条件にした。
先行発話の欠落確認が失敗した場合も、別入力世代で終了・検証済みの後続発話を処理する。

- 実Silero／libfvadへ固定speech.wavを入力し、先行終了後の確認待機中にfocus／blurと新track入力を挟むmodule回帰試験を追加した。
- 先行確認の成功／失敗の両方でPCMの不変性と発話の分離を検証した。失敗側では修正前に後続STTが滞留することを再現し、修正後に後続だけ進むことを確認した。
- このmodule試験のCore／STTと通信上の欠落確認はモックである。実VADは動作するが、実ネットワーク欠落の受入を代用しない。
- 前節の未移行43件を含め、capture、preroll、preview、STT直列化、入力抑止、text優先、SQLite履歴の既存試験を新契約へ更新した。単体capture fixtureはBE判断後の内部入口を利用し、clientのspeech通知を再許可していない。
- BE検知時計からFE発話起点を捏造しないことを確認した。実発話起点からの遅延測定はM5／M6の残作業である。

検証結果:

- 既存LiveKit関連・契約・bootstrap: **500 passed**。
- Backend unit／module全体は600秒上限で中断したため、未実行・旧fixture失敗を後続実行で補完した。現在収集される**5,640件**のtest IDを3個のJUnit結果と照合し、各IDの最終結果は**5,640 passed、0 failed、0 skipped、0 missing**だった。これは単一の中断なし全体実行の成功ではない。
- 元の一括実行は5,067 passed／1 failed／1 skipped、未実行・失敗の補完は571 passed／1 failed、最終修正後の対象実行は70 passed。元のskipはroot依存不足によるPromptfoo smokeであり、npm ci後の実行成功で解消した。
- mypy: **337 source filesで問題なし**。ruff成功。

一時JUnit結果の照合情報（SHA256）:

| 対象 | SHA256 |
|---|---|
| 現在の収集test ID一覧 | 4bee552753fa779e4d0c9755012f3c7d90ae4691e9a5075c9da9800759e9dae6 |
| ds358_be_full.xml | 15c324c8d6149f91ade873678c50f86e84a4d36322d850d0602a6b521afa709d |
| ds358_be_remaining_full.xml | 9faa2bf5fc30b6a8d716267374d45a9d0e1b3bdb088667f0088eb58314e74034 |
| ds358_final_targeted.xml | 923c99ddbbf518a2266136ff90f6315b59bb619cd7e0e07377d31111a792d352 |

実サービス・ブラウザ試験、移設前後の計測、人の実マイク・聴感受入は未完了。
