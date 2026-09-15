# #358 音声入力移設の検証記録

## 現在の範囲と未完了事項

本Epicの受入は未完了。以下はda7172fまでのコード・保存済み証跡を照合した状態である。
後続の各節は実行時点の記録であり、過去の「後続作業」「未移行」や失敗を、現在の実装状態や解消済みの証明へ読み替えない。

| 対象 | 現在の証拠と残条件 |
|---|---|
| M1の移設契約 | [設定・protocol・測定条件](../voice-backend-migration-contract.md)を固定。移設前アプリ基準は07b8b1e。基準の確定と性能測定の合格は別 |
| M2〜M4の責務移設 | BE VAD、正式発話・入力世代、音声欠落検証、Core/private 2.0、FEの継続マイクとACK認可を実装。局所試験と限定した実会話を確認。全受入シナリオの実検証完了ではない |
| M5の観測・集計 | 固定音声起点、BE sample／実STT PCMの相関、割り込み・相槌・pauseの集計を移行。通常応答・相槌・take-turn・pause／雑音・再接続の合意済み全cohort比較は未完了 |
| 完全100試行の証跡 | [移設前相槌100件](../artifacts/voice-backend-358-before-backchannel-run-100.json)を完走したが68成功・32失敗、継続の欠測16件。品質合格・回帰なしの証拠には使わない。中止runと失敗は保持 |
| M6の局所実接続 | 固定音声の3往復、旧version拒否、[開始失敗後のSession／Room解放](../artifacts/voice-backend-358-startup-cleanup-browser.json)などを限定確認。個別の対象版と範囲は各節・artifactを参照 |
| 更新・切り戻し | [履歴Repository往復](../artifacts/voice-backend-358-history-roundtrip.json)は成功。[FE／BE一括更新・切り戻し手順](../voice-backend-rollout.md)の通し実行は未実施 |
| 人の受入 | 実マイク・聴感は未実施。固定WAVやAPI成功で代用しない |
| 検査・統合 | 全体ローカルCIは908f966時点。その後のfc43694は関連94件・型検査、da7172fは実ブラウザ開始失敗1件・型検査を確認。最終版のremote CI成功とEpic統合は未達。main向けCodeRabbit差分レビュー・修正も必要 |

GPUを使う残測定は、ユーザーが#341実装後に確保する時間帯で行う。共有推論の設定や背景記憶抽出を変更して測定条件を軽くしない。
失敗・欠測・未実行を元の分母に残し、#150の既存目標・WebSocket比較、#350の既存未達と今回の回帰を区別する。

## 初期M2局所検証の範囲

以下は#394の部分実装時点の記録。連続16kHz mono PCM16から、既存Silero legacyとlibfvadでcandidate／confirmed／ended／misfireを得る処理をBEへ実装した。
この時点ではLiveKit接続・正式発話・入力世代・欠落統計・新protocolは後続作業だった。後の実装・検証を含めても、本書全体を性能受入の完了証跡とは扱わない。

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

## 2026-09-15: Frontend結合試験・モックE2Eの移行

- Roomモックは新track SIDとBEの入力開始ACKを返す。録音デバイスが有効になる前の発話代入を拒否し、clientから正式speechイベントが送られた場合も失敗させる。
- 発話／割り込みは明示的なBEモック通知へ変更し、削除済みcontroller.speechStartedへのテスト入口を除去した。
- マイクボタンは端末の有効状態、会話状態欄はBEの発話状態として検証する。focus／手動mute／text受理／再接続で新trackを開くこと、遅着出力を破棄することを保持した。
- 既存3往復ケースはFE VAD asset取得を禁止し、取得要求0件で履歴へ反映されることを検証する。発話境界はモックであり、実PCMからの3往復受入ではない。
- モックの停止時間はBE判断通知からlocal停止までの時計として記録する。旧FE VAD時計を補完せず、実発話からの性能指標とは別にした。
- Addon音声承認ケースはブラウザのテスト用マイクを設定し、BE ACKによる有効化を待ってから発話を通知する。マイク未接続でも発話だけを注入していたfixtureを修正した。
- Speech／Text履歴投影、privacy終端、bootstrapの結合試験fixtureをprotocol 2.0へ移行した。

検証結果:

- Frontend module: **141 passed、0 failed、0 skipped**。
- 音声モックE2E: **16 passed**。その後、Addon／通常チャット／画面共有等を含む全体を実行し、**56 passed、0 failed、0 skipped**（37.4秒）。
- Svelte: **0 errors / 0 warnings**。E2E／integration TypeScript検査成功。
- Frontend build成功。500kB超のchunk警告は残る。

実LiveKit／Whisper／LLM／TTS、実発話起点とBE境界の計測相関、前後比較、人の実マイク・聴感受入は未完了。

## 2026-09-15: 実LiveKit輸送・固定音声のブラウザ往復

専用のLiveKit 1.9.7を既存Composeからdev用7880で起動し、test専用data rootと
Backend／Frontendイメージを使用した。dogfoodの17880と会話データには接続していない。
共有Whisper／Ollama／VOICEVOXは既存サービスへ接続し、起動・停止・設定変更は行っていない。

### 実ブラウザの機能確認

アプリの対象commitは`0055de4`。既存の固定speech.wavをAudioWorkletから流す実サービス試験で確認した。
本文・tokenを含めない証跡と資材hashは[機能確認artifact](../artifacts/voice-backend-358-livekit-smoke.json)に記録した。

- 通常音声1往復: **1 passed**。BEの発話開始・終了、実STT／LLM／TTS、再生開始1件・再生完了1件。transport failure 0件。
- 同じSessionでの3往復: **1 passed**。発話3件、再生開始3件・再生完了3件。transport failure 0件。
- 物理マイク・人の聴感は未実施。所要時間は試験全体の時間であり、発話単位の遅延や前後性能比較には使用しない。

### 実通信試験と統計確認の競合修正

既存の実LiveKit suiteをCore／private 2.0へ移行した。client側ACK確認は許可されたsession_resumedを使い、
BE専用session_startedの偽送信を除去した。継続マイク試験は入力開始ACK後に実PCMを送り、
実CPU VAD／prerollへの到達と、Opus統計による確認を検証する。

最初の全体実行では旧fixtureが1件失敗し、修正後の実行では継続マイクの統計確認が1件失敗した。
後者は確認対象16,000〜64,000 sampleに対し、既知開始1,600・確認済み末尾174,080 sampleで、
欠落窓もないのにverifyがtimeoutしていた。確認済み範囲を参照する前にverify自身の統計再読込成功を
待つ構造が原因であり、重複snapshotの条件を使ったmodule試験でも再現した。

`caa5f5f`で、background監視の確認済み範囲を使って判定するよう修正した。
確認範囲の延長、検出済み欠落の不採用、close後の成功は許可しない。

- 修正前の新規module回帰試験: **2 failed**を確認。修正後は実VAD／入力世代／captureを含む**20 passed**。
- 修正後の継続マイク実接続診断: **5回連続passed**。
- `caa5f5f`での実LiveKit全suite: **11 passed、0 failed、0 skipped**（52.87秒）。
  bootstrap、認証期限、カメラpublish拒否、ACK／再送、連続マイク、retry超過、outbox超過、
  duplicate identity、reconnect猶予切れとRoom解放を確認した。
- mypy 337 source files、ruff、git diff --check成功。
- `0055de4`のPR #403では4つのCIジョブが成功した。上記の追加修正は更新後のCIで別に確認する。

ここで確認したのは実輸送・固定音声の限定的な機能であり、M5／M6全体の完了ではない。
実発話起点の計測相関、全cohortの同条件前後比較、割り込み・混在操作・障害時の実受入、
VAD／reader実行時障害の追加監査、更新・切り戻し、人の実マイク・聴感確認が残る。


## 2026-09-15 実行中のVAD障害と入力停止通知

`47fbbc7` の後続差分で、native推論・resetの例外経路を監査した。推論例外は
発話の回復処理を通らず、owned taskの例外としてSession全体のcleanupへ進んでいた。
また、無音中のreader EOFは未終了発話がないため入力停止を通知していなかった。

- 新しい実VAD障害注入試験は修正前に **4 failed**。推論失敗を型付き障害へ正規化し、
  未終了発話を破棄・resetして、700ms以上の静音後に次の発話へ回復するよう修正した。
- reset失敗は入力認可を破棄して世代付きの停止エラーを通知する。開始時はrequestに対応した拒否を返す。
  readerのEOF・例外・不正frame・cancelではmonitorとstreamを解放し、当該入力を停止する。
- FEでは停止通知のSID・世代・revisionを照合する。旧reader終了・旧reset失敗・旧エラー通知が
  再開後の入力へ作用しないことを確認した。
- 開始ACK後、制御送信の完了前に停止通知が届く条件では、マイクが再び有効になる不具合を
  **1 failed**で再現した。ACK受信時点で認可を保持し、入力有効化前のrevision確認で停止を維持した。

検証結果:

- BEの実VAD入力・bridge・worker・runtime audio: **103 passed**。
  外部STT等はmodule境界のstubであり、この障害試験を実サービス障害受入とは扱わない。
- FE unit: **876 passed**、module: **141 passed**。
- Svelte check: **0 errors / 0 warnings**、E2E TypeScript check成功。
- 正式なPython lint範囲 `backend/app` とmypy **337 source files**成功。
- 任意に広げた `backend/tests` 全体のruffは既存の79件で失敗。今回変更したテストファイルの指摘は0件。
  既存のテスト全体lintの課題をこの修正で解消したとは扱わない。
- JSON Schemaから共有型を再生成した。型ファイル自体の差分はなく、条件付き必須項目は
  wire validatorで検証する。FEのAJV strict設定とBEのdecodeで通知を検証した。
- 前段コミット `47fbbc7` のPR #403 CIは4ジョブ成功。今回差分のCI結果は別途確認する。

未完了: 実環境での障害注入、M5の実発話起点の計測相関、全cohortの同条件前後比較、
残る割り込み・混在操作の実受入、更新・切り戻し、人の実マイク・聴感確認。


### コンテナ検証でのnative終了競合

`f86b31b` のCI run `34887311350` で、Backendイメージ構築中の
`FfiClient.instance` 初期化チェックがexit 134（native workerのnon-unwinding panic）で失敗した。
このstageはアプリコードのCOPY前であり、VAD処理の実行中ではない。

- 既存の専用Backendイメージをnetworkなしで起動して同じコマンドを繰り返すと、
  5回目にexit 139と同じpanicを再現した。成功4回だけで一過性・解消済みとは判定しなかった。
- 固定版[FFI dispose](https://github.com/livekit/rust-sdks/blob/63128d01d955d9d8967544f46cff64a361232bf6/livekit-ffi/src/cabi.rs)と
  [server終了処理](https://github.com/livekit/rust-sdks/blob/63128d01d955d9d8967544f46cff64a361232bf6/livekit-ffi/src/server/mod.rs)、
  導入済みPython 1.1.16のatexit登録を照合した。Python終了とnative callbackの競合が疑われる。
- build検証内でSDKのFFIを明示disposeする条件では、同一イメージで **30回連続exit 0**。
  Dockerfileの初期化・version確認を維持したまま、この解放を追加した。
- SDK版・native patch・モデル設定は変更していない。これは短時間で終了するbuildチェックの
  修正であり、稼働中の実会話や全SDK終了経路が検証済みであるという意味ではない。

- 変更したDockerfileで専用Backendイメージの構築成功。初期化・disposeのRUNを実行し、
  SDK build cacheを保持した。実ブラウザでの回帰確認は後続の計測移行と合わせて行う。


## 2026-09-15 BE判断に対応した割り込み観測の移行

既存Playwright driverの割り込み記録が、削除済みのFE VAD由来
`speechStartedAtMs` を必須としていたため、BE判断による実停止が収集されなかった。
同じdriverへ、responseに対応した実停止・BE判定受信・取消確認の収集を接続した。

- ローカル停止callbackが判定通知より先に届く場合と、取消確認が停止より先に届く場合を扱う。
  観測順序6通り・別response・重複・上限超過のunit試験 **9 passed**。
- BE speechイベントのSID・入力世代・sample境界・BE時刻を本文なしの診断へ保持する。
  client到着時刻とBE時刻は別fieldで保存し、直接減算しない。
- FE VAD由来の時刻がない場合はnullのまま残す。固定音声の実barge-in試験では、
  workletで観測した正解speechStartの上下限から停止・取消確認の遅延範囲を算出する。
- 継続マイクの発話終了試験は、廃止したFE VADのCSS状態に代えて、
  同じutterance・SID・入力世代のBE開始／終了通知を確認する。
- 収集上限超過と、複数の異なる停止・判定が同じresponseへ来る曖昧さを明示する。
  実停止を観測していない取消だけから停止時刻を作らない。

全体検証は、初回にVitest worker 1個が異常終了して既存RTPテスト9件の結果が欠けた。
この実行を全体成功とは扱わずログを保持した。対象を変えずworker数を4に制限した再実行で
**FE unit 885 passed / module 141 passed / Svelte 0 errors・0 warnings / E2E型検証成功**。

これは計測移行の一部である。実ブラウザでの新probe確認、pause cohortのFE frame依存解除、
集計側の旧client起点から正解境界・BE境界への移行、全cohortの前後比較は未完了。


### 新probeの実ブラウザ確認

`92a1f6e` を専用integration-voice Profileで実行した。
- BE発話開始／終了を確認し、継続マイクを維持する試験: **1 passed**（4.3秒）。
- ラベル付き実barge-in: **1 failed**。BEのtake_turnと実停止・取消確認は収集できたが、
  正解発話開始から停止までの上限が **6,166.5ms** となり、既存3,000ms基準を超えた。
  これは単一試行の基準違反であり、移設前とのp95回帰を確認した結果ではない。
- 初回の失敗ではlatency添付が閾値assertの後にあり、失敗時に詳細を残せなかった。
  正解境界・停止・取消の添付を判定前へ移し、afterEachにも本文・tokenを除いた相関と時刻を残す。
- 次の診断では既存controlled traceを併用し、VAD・preview STT・判定・通知の内訳を調べる。
  3秒／3.5秒の基準は変更しない。


`0a1e198` で詳細添付と既存controlled traceを使った再実行は **1 passed**。
正解境界から実停止まで **1,148.4〜1,152.3ms**、取消確認まで **1,203.4〜1,207.3ms**。
BEの同一時計内では、発話検知からpreview開始まで約290ms、
preview開始からtake-turnまで約255ms、take-turnからserver取消まで約37msであり、
この試行は発話終了前のpreview判定で停止した。

初回6,166.5msの失敗にはstage traceがないため、その原因が解消されたとは扱わない。
同じ基準に対する失敗・成功の両方を[割り込み診断artifact](../artifacts/voice-backend-358-interruption-smoke.json)へ残した。
追加診断の最初の起動は、suiteが固定するdata rootとtrace pathの不一致でreadiness失敗した。
試験開始前のsetup失敗として区別し、既存test領域内の新規trace pathへ修正してから実行した。

今回起動した専用LiveKit・Backend・Frontendは停止済み。7880／7881／8000／5173／4174の閉鎖を確認し、
共有Ollama／VOICEVOX／Whisperと別タスクのOllamaは継続している。


### M5: take-turn集計の固定音声起点への移行

既存のtake-turn reporterを、scheduled fixtureの正解発話開始とBE正式発話の相関で
集計できるようにした。主指標と合否には遅延の上限を使い、上下限と全分母を保持する。
旧FE VAD起点の保存済み集計は引き続き検証可能であり、同一起点の前後比較とは区別する。
不完全なメタデータ、別起点、楽観的な上限、分母の不一致をcohort validatorで拒否する。

- take-turn／cohort schema／相槌／通常遅延／VAD／pilot関連のunit: **213 passed**。
- 変更PythonのRuff、差分空白検査: 成功。
- Svelte／E2E TypeScript: **0 errors / 0 warnings**。
- 今回の追加は集計・ハーネス・仕様の接続。実サービスでの新report出力、
  pause cohort移行、100試行の前後比較と人の実マイク確認は引き続き未完了。


`618177c` で既存run_pilotのtake-turnを1試行実行し、実Browser／LiveKit／Whisper／
LLM／VOICEVOXで **1 passed、skip 0**。新しいreporter・cohort schemaを通して、
正式BE発話との相関、実停止、取消確認、session終了を欠測なしで確認した。

- 正解発話開始から実停止: **1,979.7〜1,981.9ms**。
- 正解発話開始から取消確認: **2,040.7〜2,042.9ms**。
- BE同一時計のtake-turnから取消: **28.1ms**。
- [匿名集計](../artifacts/voice-backend-358-take-turn-pilot.json)は全1件を分母として保持する。
  既存遅延基準は通過したが、100試行に届かないため見逃し率の受入はfalseのまま。
- [診断証跡](../artifacts/voice-backend-358-interruption-smoke.json)に実行commit、
  fixture・raw manifest・trace・native SDK・runtime結果のhashを追加した。
  先の6,166.5ms失敗は削除せず、原因解消・回帰なしとは扱わない。
- test専用LiveKit／Backend／Frontendを停止し、7880／7881／8000／5173／4174閉鎖を確認。
  共有11434／50021／50022と別タスク11438は継続。

保存前の検証で、新起点のraw manifestにinput_authorityがないとfrontendを補完する箇所を
追加の反例テストで検出した。新起点では判断主体を明示必須にし、不完全な証跡を拒否する。
過去のメタデータなしFE artifactの解釈は維持する。

最終確認: 関連unit **214 passed**。保存した匿名pilot reportも現行cohort validatorで再検証済み。


### M5: pauseハーネスのBE境界への接続

pause cohortは新しいBE版でFE VAD frameを待たず、同一session・track・入力世代の
正式speech_started／speech_stoppedと最終utteranceを照合する。
二つの発話を一つに統合せず、重複・対応なし・逆転・overflow・破棄は診断失敗として保持する。
旧FE版比較はinput_authority=frontendを明示して旧ハーネスの境界観測を維持する。

診断件数だけで音声全体の到着や語頭・末尾を証明しない。
BE通知のserver実行時刻からsample数を引いてbrowser上の発話時刻を合成しない。
旧VAD境界reportはBE入力をmedia照合未了として欠測にし、旧FEイベントが含まれても代用しない。
実PCMの端部確認には既存のobserve-stt-pcm／report_stt_pcmを併用する。
BE detector sample位置とsource PCMを結ぶ境界offset集計は引き続き未完了。

検証: 新しいハーネス用unit **13 passed**、VAD／STT PCM集計 **39 passed**、
Svelte／E2E TypeScript **0 errors / 0 warnings**。


`76b042f` の実pause pilotは3件中2件成功、1件失敗。
400ms／600msの休止では正式発話が各1件で、Whisperへの最終入力と固定音声の語頭・末尾も
既存v4 PCM照合で確認できた。200msの試行は初期応答用の発話が
audio_integrity_unavailableで破棄され、ラベル付き音声の投入へ進んでいない。
[3件全体の証跡](../artifacts/voice-backend-358-pause-smoke.json)と
[PCM集計](../artifacts/voice-backend-358-pause-pcm-pilot.json)に失敗を含めて保存した。

受信統計の最初の有効観測より前に入力開始ACKを返し得る順序がコード上にあったため、
認可trackの統計準備をACK前に待つ。FEの5秒deadlineに収めるためBEの準備上限は4秒とし、
準備できなければ入力開始を拒否する。過去の未観測範囲を正常へ遡及補完しない。
この変更だけで今回の初回失敗原因を特定・解消済みとは扱わず、同じ実3試行で再確認する。

局所検証: 統計準備の待機・終了・取消とruntime **76 passed**、
正式Ruff成功、mypy **337 files成功**。


統計準備待ちを追加した `0f0a028` の同じ3試行でも、初回は同理由で失敗し、
後続400ms／600msの2件は誤分割なし・実PCM端部一致だった。
[2回目のPCM集計](../artifacts/voice-backend-358-pause-pcm-pilot-ready.json)にも
全3件を残す。準備待ちを実測失敗の修正完了とは扱わない。

原因を絞るため、統計確認のtimeout／失敗時に要求sample範囲・確認済み範囲と、
統計取得失敗・ID変更・stale snapshot・counter逆行の件数を既存のutterance traceへ記録する。
本文・PCM・統計の識別子は転記しない。失敗・閾値・待機上限の判定は変更しない。
追加後の局所検証: **77 passed**、正式Ruff・mypy成功。


`3a447f4` の数値診断追加後、独立した1件runと5件runを実行した。
いずれも全件で正式発話は各1件、最終STT・実Whisper入力と語頭／末尾のv4照合に成功し、
session終了まで確認できた。5件の最小端部相関は約0.945。
[1件のPCM report](../artifacts/voice-backend-358-pause-pcm-diagnostic-01.json)と
[5件のPCM report](../artifacts/voice-backend-358-pause-pcm-diagnostic-02.json)は欠測0だが、
100件条件を満たさないためpassedはfalseのまま保持する。

この2 runでは統計欠測が再現しなかったため、追加した数値traceによる根本原因の確定には
至っていない。先の失敗2件は保持し、初回発話の問題を解消済みとは記録しない。
[全runの証跡](../artifacts/voice-backend-358-pause-smoke.json)へ実行版とhashを追加した。
BE detectorのsample位置とsource PCMの境界offset集計、全cohortの同条件100試行比較、
実マイク・聴感、切替／切り戻しは未完了。

今回の専用LiveKit／Backend／FrontendとPCM中継は停止済み。
7880／7881／8000／5173／4174に加えて50023の閉鎖を確認し、
共有推論サービスと別タスクのOllamaは維持している。

### M5: BE sample境界と実STT入力の対応付け

正式開始frameでtrack sampleとBridge受信連番を対応付け、終了frameでawait前に固定する。
captureの連続範囲とSTT前処理のprefix除去を介して、既存PCM observerの二つの独立anchorと
v4端部照合へ接続した。offsetはmedia軸で算出し、異なる時計の減算は行わない。
確認不能・不一致は欠測とし、誤分割件数はPCM照合可否から独立して保持する。

- capture lineage、既存PCM／VAD report、新BE report・CLIの局所試験: **75 passed**。
- 正式Ruff成功、mypy **337 files成功**。
- 初回局所試験で既存testの定数import削除を検出し修正した。実装の成功へ読み替えず再実行で確認した。
- この段階は合成証拠と局所回帰の確認。新しいsample対応を記録する版での実接続確認は後続とする。

`1f64a08` の実Browser／LiveKit／Whisper／Ollama／VOICEVOXでpause 3試行を実行し、
**全3件成功、欠測0、誤分割0**。新しい[BE VAD集計](../artifacts/voice-backend-358-pause-vad-pilot.json)と
[実PCM集計](../artifacts/voice-backend-358-pause-pcm-media-pilot.json)の両方で同一3件の対応を確認した。

- 3件の開始offsetの下限～上限全体は約 **-52.8～-3.1ms**。
- 検知終了offsetの下限～上限全体は約 **647.2～704.9ms**。
  固定音声の末尾から検知終了までのmedia軸上の差であり、応答遅延ではない。
- 語頭／末尾基準違反0、境界不確定0、cleanup 3件、端部最小相関約0.934。
- 100試行条件は未達であり、両reportのpassedはfalseを保持する。
- raw manifest、trace、PCM observerのhashを匿名reportに保持した。
  前の初回発話失敗2件は今回再現しなかったが、原因を解消済みとは扱わない。

専用LiveKit／Backend／Frontendと50023のPCM中継は停止済み。
7880／7881／8000／5173／4174／50023閉鎖、共有推論サービス継続を確認した。
全cohortの同条件前後100試行、人の実マイク・聴感、切替／切り戻しは引き続き未完了。

### M5: 相槌の観測上限超過を成功から除外

相槌reportがcore event／停止probeの上限超過を確認せず、全出力証拠だけで
誤停止なしにできる経路を反例 **7 failed** で再現した。
BE版は両probeの完全観測を明示必須にし、欠測・不正値・overflowなら未計測へ残す。
既に確認できた取消・実停止は観測が不完全でも失敗件数へ残す。
旧FE artifactは既存のflagなし形式を維持し、明示overflowは未計測にする。

修正後、相槌・take-turn・VAD集計の関連試験 **108 passed**、変更ファイルRuff成功。
全100試行の相槌受入を意味するものではない。

### M5: 実相槌pilotと100試行の中止記録

`9ebc209` の実Browser／LiveKit／Whisper／Ollama／VOICEVOXで相槌3試行を実行し、
BE相槌判定・旧回答の全出力継続・session終了を全3件で確認した。
[相槌集計](../artifacts/voice-backend-358-backchannel-pilot.json)は誤停止・取消・欠測0。
[実PCM集計](../artifacts/voice-backend-358-backchannel-pcm-pilot.json)も端部一致3件、欠測0。
全体位置ずれが非一様な1件は端部一致とは別に残す。100件条件は未達のまま。

同じ版・固定順で100試行を開始したが、run中にGPU使用メモリが約5.2GBから16.5GB、
使用率がほぼ100%へ変化し、共有Whisperが504の後に503を繰り返した。
同条件比較を成立させられないため、所有確認したPlaywrightへSIGINTを送り終了した。
GPU使用量変化とHTTP障害の因果関係・負荷を発生させたプロセスは未確定である。

[中止runの匿名証跡](../artifacts/voice-backend-358-backchannel-interrupted.json):

- 期待100件、記録済み16件（成功8・失敗8）、未記録84件。
  PCM観測には17件目の開始が含まれるため、84件をすべて未開始とは扱わない。
- 記録済み16件のsession終了を確認。中止された17件目の試行成功は補完しない。
- 失敗内訳: 再生継続確認3件、初期応答5件。
  前者には相槌判定があり、実停止・取消・transport failureの記録はないが全出力完了証跡がない。
  後者にはstt_upstream_failedが記録されている。
- PCM中継の上流HTTP観測: 200が41件、504が2件、503が10件。
  最大観測待ち時間は約46秒。HTTP要求数は試行数とは区別する。
- pilot終了コード130。途中runを完全cohortのreporterへ投入して合格にしない。
  raw manifest・trace・PCM observer・資源観測・中止記録をhashとともに保持した。
- 専用LiveKit／Backend／Frontend／PCM中継を停止し、
  7880／7881／8000／5173／4174／50023閉鎖を確認。共有推論・別タスクのOllamaは変更していない。

### 移設前比較用ハーネスの準備

専用branch `codex/358-baseline-measurement`、commit `67e1a03` を作成した。
アプリ基準 `07b8b1e` のbackend／frontend/src／contracts／environments／characters／infraを保持し、
測定ハーネスとreporter・schemaだけをそろえた。旧FE版のinput_authorityはfrontendを明示する。
旧controller hookとマイク待機の観測を残し、旧FEのVAD判断をBE版へ変えていない。
基準tree、計測コード出典、検証範囲を同branchの匿名artifactと日本語文書に保存した。

比較ハーネスの局所検証: 旧版集計62件成功、Svelte 0 errors / 0 warnings、E2E型検査成功。
実サービスでのbefore測定・100試行前後比較は未実施。

### M6: 保存履歴の旧版・新版往復と更新手順

保存履歴実装とruntime data pathに `07b8b1e` からの変更がないことを確認したうえで、
新規一時SQLiteを旧版 `67e1a03`（アプリ基準 `07b8b1e`）と新版 `09653c5` の
実Repositoryから別プロセスで読み書きした。既存の会話・dogfoodデータは使用していない。

- 旧版で完了・中断prefix・privacy除外の3turnと手動タイトル・archiveを作成。
- 新版で既存状態を検証して1turn追加。旧版へ戻してその追加を確認し、さらに1turn追加。
- 新版から全5turnを確認。4段階ともschema 9のままで、切替前後の全テーブル行hashが一致した。
- character境界、privacy除外本文・policy情報、保存済み中断prefixを確認した。
- [匿名artifact](../artifacts/voice-backend-358-history-roundtrip.json)と再実行CLIを保存した。
  後者でも同じ4段階の確認が成功した。既存artifactは上書きしない。
- 旧client拒否・bootstrap API、履歴schema・状態遷移の関連試験 **91 passed**。
  API module試験のLiveKit資源はstubであり、実SFUへの旧client拒否の証跡とは区別する。

[一括更新・切り戻し手順](../voice-backend-rollout.md)を既存Environment CLIに沿って整理した。
同じdata rootとphase別run reportを使用し、所有FE／BEのみ停止する。
通しの実切替、全cohortの前後100試行、人の実マイク・聴感は未実施のまま。

### ローカルCI全体の再確認

`908f966` で正式CIのunit／module／生成契約／型／build／mocked E2E範囲を検証した。
[匿名集計](../artifacts/voice-backend-358-local-ci.json)に各チェックの終了結果とログhashを保持した。

- Backend unit: 初回 **3928 passed / 1 failed**。音声移設で変更していないcharacter lifeの
  reflection invalidation試験で、更新時刻が作成時刻より約1,283ms前になっていた。
  当該テストは固定baselineで単独成功したが、時刻逆転の根本原因は未確定である。
- 同じunit全体の再実行: **3929 passed**。初回失敗を削除・解消済みへ読み替えない。
- Backend module: **1797 passed**。
- Frontend unit: **898 passed**（4worker）、module: **141 passed**。
- mocked E2E: **56 passed**。
- 生成契約の再生成差分0、mypy337ファイル成功、Svelte 0 errors / 0 warnings、E2E型成功。
- Application dev／dogfood・Whisper・IrodoriのCompose契約検証成功。
  FE／BE imageは検証commit専用タグでbuild成功。サービス起動・配備は行っていない。
- 旧アプリ基準の比較用 `67e1a03` も専用FE／BE imageのbuild成功。
  同じ固定fixture 300件のhashを照合し、比較用worktreeから参照できるようにした。

これらはローカル結果であり、未pushのremote CI成功・Epic統合の承認条件を満たしたとは扱わない。
Frontendの500kB超chunk警告と既存の非推奨警告は残る。

### 共有Whisperの復旧確認

GPU使用量が約5GB・使用率0%に戻りreadiness 200を確認した後、
既存PCM observerと同じ固定音声変換で1回の実STTを実行した。
HTTP200・空でない転写を確認し、[数値証跡](../artifacts/voice-backend-358-whisper-availability.json)を保存した。
サービスの再起動・設定変更は行っていない。
追加変換ライブラリ・外部変換器がないことによる診断準備の失敗は推論失敗に数えず、
既存変換を再利用して確認した。1回の成功を継続安定性や100試行合格へ読み替えない。

### 移設前比較用ハーネスの実接続確認

比較用 `67e1a03`（アプリ基準 `07b8b1e`）を実Browser／LiveKit／Whisper／Ollama／VOICEVOXで実行した。
input_authority=frontendを明示し、旧FE VADのアプリコードと資産を保持したまま相槌3試行に成功した。

- [旧FE版の相槌集計](../artifacts/voice-backend-358-before-backchannel-pilot.json):
  相槌判定3件、旧応答の全出力継続3件、実停止・取消・欠測0件。
- [旧FE版のPCM集計](../artifacts/voice-backend-358-before-backchannel-pcm-pilot.json):
  最終STT入力と端部一致3件、欠測0、最小端部相関約0.993。
- session終了3件、専用アプリ・LiveKit・50023中継の停止を確認。
  LiveKitはbaseline worktreeのComposeから所有コンテナを再作成したため、
  新しいIDとproject／serviceラベルを確認して停止した。共有サービスは変更していない。
- 移設前後の推論設定例、integration-voice／PCM Profile、固定音声metadata／ラベルmanifest、
  run_pilot.pyの計6ファイルが同一であることを確認した。
- 旧版・新版とも少数pilotであり、100試行前後比較・全体性能合格を意味しない。
  中止済みの新版100試行runは保持する。

### M5: 移設前100試行の中止と共有推論条件の変化

比較用 `67e1a03`（旧アプリ基準 `07b8b1e`）で、固定順の相槌100試行を開始した。
測定中に共有Whisperの504・503とGPU負荷上昇が再発したため、
run ID・cwd・Playwright引数で所有確認したrunnerへSIGINTを送り終了した。
[中止runの匿名証跡](../artifacts/voice-backend-358-before-backchannel-interrupted.json)を保持する。

- 記録済み41件（成功19・失敗22）、記録済みSessionの終了41件、未記録59件。
  PCM観測には42件目の開始があるため、59件をすべて未開始とは扱わない。
- 失敗は相槌判定／再生継続の確認18件、初期応答4件。
  最終判定の記録は相槌21・判定不能16・未観測4件。判定不能を相槌成功へ読み替えない。
- 21件目の固定音声はack-03（「ええ」）に切り替わる。
  この位置以降で判定不能が増えたが、fixtureとの一致だけから認識誤りの原因を断定しない。
- STT中継のHTTP観測は200が139件、504が2件、503が6件。要求数と試行数は区別する。
- 常駐Ollamaのcontextは8,192と36,864を交互に観測した。
  今回のtraceに残る生成要求38件はすべて8,192だった。
  別contextの要求元・GPU負荷の発生元と、Whisper障害との因果関係は未確定である。
- 開始・終了のモデルdigest（5用途）、Whisper revision／設定、サービスimage、
  推論設定例・Profile・fixture metadata／manifestのhashは一致した。
  これは測定中の負荷安定性や、Whisper model cache全体のbytes同一性の証明ではない。
- pilot終了コード130、wrapper終了コード1。専用LiveKit／Backend／Frontend／PCM中継の
  7880／7881／8000／5173／4174／50023閉鎖を確認。共有サービスは変更していない。
- 開始前の確認には旧LiveKit IDを参照する手順が一部残っていたため、
  現行IDとCompose project／serviceを確認して観測記録へ残し、次回の確認手順を修正した。
  この準備上の不一致を、会話試行の成功・失敗へ混ぜない。

全100試行の集計・前後比較は未成立のまま。今回の途中結果を完全cohort用reporterへ投入せず、
記録済みの失敗を後続の成功試行で置き換えない。
再測定では、モデル・設定の一致に加えて常駐contextの切り替わりと共有STTの継続可用性を確認する。

### M5: 試験自身の記憶抽出負荷と観測上の訂正

中止runの専用SQLiteを読み取り専用で確認した。
[記憶抽出の匿名監査](../artifacts/voice-backend-358-background-inference-audit.json)では、
抽出ジョブ38件（SAVED 24・EMPTY 1・FAILED 1・PENDING 12）と、
保存済み記録29件を確認した。既存の実会話・dogfood DBは使用していない。

- 旧アプリでも完成したCore turnから記憶抽出をsubmitし、schedulerを起動する。
  `RAG_ENABLED=false`はこの処理を無効化する設定ではない。
- 同じOllama modelに対し、CHAT／PRIVACY／MEMORY_CONSOLIDATIONは8,192、
  MEMORY_EXTRACTIONは32,768＋4,096＝36,864を設定している。
  記憶clientはこのTargetの上限を使用する。
- よって前項の「常駐contextがCHAT予定値から変化した」という観測だけでは、
  他タスクの干渉・構成変更・同条件比較の不成立を証明できない。
  試験自身の背景処理も要求元になり得る。個別要求との相関、Whisper障害の原因は未確定である。
- 既存`run_pilot.py`へ、同一CHAT modelを共有する生成用途別context設定の数値記録を追加した。
  provider／model名、任意の設定文字列・秘密情報を出さず、設定欠落を推測で補わない。
  従来の`expected_context_tokens`はCHATの予定値として保持する。
- 追加8試験の未実装時失敗を確認後、pilot関連 **91 passed**。
  これは観測メタデータの検証であり、100試行の受入結果ではない。

推論モデル・上限・記憶抽出の動作は変更していない。
次回は同じ背景処理を含む構成のまま全試行の失敗・欠測を記録する。
context差だけを外部異常として中止せず、停止が必要な場合は実際の障害・影響を根拠とする。
既存中止runの失敗や終了コードは保持し、今回の原因候補発見で解消済みとは扱わない。

同じ観測・テスト変更を比較用branchへ適用した（07269e5）。旧版でも **91 passed**、
07b8b1eからbackend/app・frontend/src・contracts・environments・characters・infraの差分0を確認した。

### M5: 移設前の相槌100試行を完走（品質未合格）

比較用07269e5（旧アプリ基準07b8b1e）で固定順の全100件を記録した。
[run証跡](../artifacts/voice-backend-358-before-backchannel-run-100.json)に
raw manifest・trace・PCM観測・資源観測・開始終了identity等のhashを保持する。
実Browser／LiveKit／Whisper／Ollama／VOICEVOXを使用し、記憶抽出を含む構成を維持した。

- 成功68・失敗32、独立Session 100件、終了確認100件。
  失敗段階は相槌判定／再生継続確認25件、初期応答7件。成功100件のPlaywright検証は失敗した。
- [相槌集計](../artifacts/voice-backend-358-before-backchannel-100.json):
  相槌判定74・判定不能19・最終判定未確認7件。実停止・取消の観測は0件だが、
  継続成立84件・欠測16件（全出力継続未確認9、fixture注入未確認7）でcoverage gateは未達。
  欠測を取消なしへ補完しない。
- [PCM集計](../artifacts/voice-backend-358-before-backchannel-pcm-100.json):
  最終HTTP入力との相関93件、端部一致91件、欠測9件。
  内訳は最終HTTP入力の一意性未確認4、最終発話未確認3、端部未確認2件。
  全体位置ずれの非一様4件を端部一致とは別に残した。
- PCM reporterはschema検証済みJSONを書いた後、品質gate不成立をexit 1で示した。
  一時集計ラッパーはこのexitをassertで止めたため、生成済みJSONを直接確認して保存した。
  JSON生成例外・全試行の中止とは扱わない。
- STT中継の上流HTTPは200が374件、504が2件、503が4件。最大観測待ち時間は約46.2秒。
  HTTP要求数と失敗試行数は同じではなく、全初期応答失敗をSTT由来と断定しない。
- [開始終了identity照合](../artifacts/voice-backend-358-before-backchannel-identity-100.json):
  モデルdigest（5用途）、Whisper設定、サービスimage、固定設定・fixtureのhashが一致した。
  旧アプリの保護対象treeも基準と一致した。
- GPU使用量の最大は約16.5GB。途中の補足観測では別Ollama（11438）にも
  GPU上のmodel常駐を確認した。測定内の記憶抽出に加え、外部の共有GPU利用もあり得るが、
  一時点の常駐から各障害の原因や全区間の負荷を確定しない。
- 専用LiveKit／Backend／Frontend／PCM中継を停止し、
  7880／7881／8000／5173／4174／50023閉鎖を確認した。共有推論サービスは変更していない。

全100件の収集完了であり、相槌品質・PCM品質・前後比較の合格ではない。
モデル・設定の一致だけから同等の負荷条件を主張せず、移設後の同条件測定と資源差の確認が残る。
以前の3試行pilot・中止runを今回の成功試行で置き換えていない。

### M6: 旧client拒否と実SFUの資源境界

アプリ10a5286の実装に対し、既存LiveKit integration suiteへ
旧版拒否4パターンと正常版の正の対照を追加し、**1 test passed**を確認した。
[匿名証跡](../artifacts/voice-backend-358-legacy-bootstrap-real.json)にテストソース・JUnit・logのhashを保持する。

- Core 1.1（private省略）、Core 2.0にprivate省略／1.0／0.0の4パターンはHTTP409。
  Session予約、Room作成、runtime接続、token発行の各呼び出しはすべて0回だった。
- 呼び出し計数は元の実装へ必ず委譲する。外部資源の戻り値をstubへ置き換えていない。
  各拒否の後に実LiveKit APIからRoom一覧が変わっていないことも確認した。
- 同じ実portsで現行Core／private 2.0のbootstrapは200となり、
  実Python RTC clientが実SFUへ接続した。userとcharacterの2participant、Room解放を確認した。
  正常版では各資源の作成経路が実際に呼ばれたため、未設定による拒否だけを成功と扱っていない。
- bootstrap検証区間のgenerate_text／generate_structured／embed／estimate_input_tokensは各0回。
  マイクtrackはpublishしていない。共有推論を性能測定に使用した結果ではない。
- pytestの新規一時data rootを使用し、専用LiveKitのみ停止した。
  7880／7881／8000／5173／4174閉鎖、共有Ollama／Whisper／VOICEVOXの稼働継続を確認した。
- API側はFastAPI TestClientで実行した。配備済みHTTPサーバーとブラウザ旧clientの操作、
  FE／BE一括更新・切り戻しの通し確認は別途残る。変更テストのRuff・差分checkは成功。

### 実HTTP・ブラウザでの旧version拒否

frontend/integration/voice/bootstrap-rejection.spec.tsを追加。新FEのbootstrap version提示だけをCore 1.1／private省略・1.0・0.0へ変更し、実BEの409、汎用エラーと入力停止、拒否後1秒のマイク取得・VAD asset要求・新WebSocket 0件を4ケースで確認した。HTTP応答・音声clientはmockへ置換していない。型検査も成功。

[匿名化記録](../artifacts/voice-backend-358-bootstrap-browser.json)に4件のattachment、run／テストsource hash、途中の試験失敗とcleanupを保持した。旧FE bundleそのものの操作と、実SFU内部の資源作成呼び出し数は別の確認範囲である。全cohort比較と人の受入、FE／BE一括更新・切り戻しを完了した扱いにはしない。GPU計測はユーザーが#341の実装完了後に確保する時間帯で行う。

### 接続開始中の取消・失敗に対する資源所有

GPUを伴わない終了境界の確認で、token取得後のRoom生成／接続／開始event送信に失敗した場合と、終了操作後にtokenが届く場合に、開始処理が予約Sessionへの終了APIを呼ばない経路を確認した。また、終了後の遅延成功が呼び出し元へ成功を返し、マイク取得へ進める経路があった。

接続確定前のbindingとRoomを開始処理が所有するようにし、失敗・取消後はその資源だけを回収する。controllerへ所有を移した後に終了操作が走った場合は二重解放しない。取消は失敗として返し、接続失敗後の古いobserverでerrorから再接続中へ戻さない。終了API自体が失敗した場合は元の開始失敗を保ち、終了成功と扱わない。

controllerの新規8ケースを含む48件、AudioRecorderの取消後マイク取得／VAD／publish 0回を含む2件、Appの44件が成功した。修正前は取消2ケースが成功を返し、Room生成／接続／開始event失敗の3ケースで終了APIが0回となることを再現した。Svelte／TypeScript検査はエラー・警告0件。これらはunitの呼び出し・状態検証であり、実SFUの障害注入や実マイク受入の証拠ではない。実接続試験と次回性能計測には、この修正を含む新版FE／BEを使用する。

### 開始失敗時の資源解放を実ブラウザ・実SFUで確認

fc43694の修正を含むFE／BEを同じ専用Environmentで起動し、実bootstrap後の接続失敗を確認した。 再実行時はPlaywrightの実行環境にも同じLIVEKIT_URL／LIVEKIT_API_KEY／LIVEKIT_API_SECRETを設定し、integration-voiceからstartup-cleanup.spec.tsだけを指定する。管理用認証はNodeから起動するPythonの環境変数で使い、ブラウザやartifactには渡さない。token応答の接続先だけを、試験自身の一時TCP portへ変更し、到着した接続を直ちに閉じる。実SFUの参加者APIでRoom 1件・BE参加者1人を確認してから障害接続先をブラウザへ返す。

1件成功。ブラウザの接続2回が失敗し、製品側からのSession終了API 1回が200を返した。テストの追加cleanupより前に実SFUのRoom 0件と、同じSessionへの再接続409を確認した。UIはエラー・入力停止、マイク取得は0回。稼働中FE／BEのcontainer IDとimage IDを採取し、Environmentのrun reportと照合した。型検査も成功。

[匿名化記録](../artifacts/voice-backend-358-startup-cleanup-browser.json)に元の8証跡hashと初回失敗を保持する。初回はRoom一覧の参加者数0を事前assertで拾い、テスト側cleanupもTest endedで失敗した。初回専用DBの空会話1件を保持し、Environment所有FE／BEと専用LiveKitは停止した。成功runの会話・turn・memory job／receiptはcleanup後0件。両runとも共有サービスは停止・変更していない。

これは起動失敗1ケースの実検証であり、全接続競合、FE／BE一括切替、性能比較、人の受入を完了した証拠ではない。GPU計測は#341実装後の確保時間帯で行う。

### remote CIで判明した局所診断の分類修正

e6e9398のremote CIで、追加したbootstrap-rejection／startup-cleanup診断が通常の実接続スイートの通信置換禁止に抵触した。両診断は実BE／SFUを使う一方、HTTPのversionまたは接続先を制御するため、frontend/diagnostics/voice-startup/と専用Playwright入口へ移した。禁止検査は緩和せず、通常のintegration-voiceが診断を収集しないことも検証する。

過去artifactのsource path／hashは当時の版の証拠として保持する。実HTTP・SFUで確認した部分と、通信を制御した部分を区別し、これら5ケースを無変更clientの実接続受入の代用にしない。M5／M6の残条件は変更しない。
