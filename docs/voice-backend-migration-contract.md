# #358 音声入力移設の実装基準・protocol・計測契約

## 状態・照合基準

M1 / #393の実装基準。責務と一括移行の判断は[ADR](decisions/voice-backend-authority-2026-09.md)、
全体要件は[指示書](epic-358-voice-backend-requirements.md)を参照する。
本書はruntime移設・実音声受入・性能実測の完了記録ではない。

責務移設はPR #408でmainへ反映済み。以下の移設前基準と移設時の契約は保持する。
導入済み機能と残受入の対応は[残受入対応表](validation/voice-quality-350-423-424-status.md)、
#350・#423・#424の新しい改善版の固定・空状態→記憶参照の評価条件は
[共通実行指示書](voice-quality-350-423-424-requirements.md)を参照する。
旧測定を新しい条件での性能証拠へ読み替えない。

2026-09-15のorigin/main `07b8b1ee638ca222afe61983ebaf17c6edaaf1f7`を移設前のコード基準とする。
Epic `4e00405a017e959300fe89942c0e3d5a24d46a76`との差は文書のみ。
以後の比較はこのコードを保持したworktreeと、移設後のコミット済みworktreeで行う。
対象と無関係なmainの更新を片側だけへ混ぜない。

## 移設前基準の実装・有効値

| 対象 | 確認したコード・値 | 移設時の扱い |
|---|---|---|
| 主VAD | `AudioRecorder.svelte`のcontinuous経路はSilero legacy。16kHz、1536 sample / 96ms frame | 同じモデルをCPU ONNX Runtimeで実行する |
| モデル | `@ricky0123/vad-web` 0.0.30同梱、1,807,522 bytes、SHA256 `a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28` | hash固定・配布元とライセンスを同梱し、runtimeで検証する |
| 主モデル状態 | input float32 [1,1536]、h/c float32 [2,1,64]、sr int64 16000、output/hn/cn | モデル本体を共有してもh/cは入力Sessionごとに分離する |
| 短発話補助 | libfvad 16kHz / mode 0 / 160 sample。既存WASM SHA256 `3fadafc9c5c1c3117d0178ae631484c6dabc72e0f8742b95c370cd39f46cf171` | 同じWASMをWasmtimeで実行する。別実装のWebRTC VADへ置換しない |
| スペクトル補助 | 1024点Hann、上位2つの5-bin帯域集中度・平坦度 | NumPyで同じ窓・bin・floor・集中度を算出する |
| 発話区間 | `utterance-detector.ts`のcandidate/confirmed/ended/misfire | 同じframeと確率・補助値に対する状態遷移を比較して移設する |
| PCM活動 | RMS ≥ 0.001、活動100ms以上 | 維持する |
| 確率根拠 | strong ≥0.3 / 300ms、short strong 250ms、high ≥0.4 / 192ms、evidence ≥0.2 / 積分140ms、証拠窓2000ms | 維持する |
| 終了 | PCM静音600ms超、neural probability <0.25の700ms超。確定後の補助有声根拠でneural静音加算を抑止 | 文中の間と短発話待機を含め維持する。旧ADRの1400msを代入しない |
| 短発話の終了時補助 | 活動span ≤1000ms、有声160ms以上・比率≥0.6、集中度<0.9、平坦度<0.3 | 維持する |
| idle reset | RMS <0.001の静音700ms、reset間隔256ms。reset後も同じframeを処理 | 主モデルと補助モデルをresetし、入力PCMは切り落とさない |
| preroll | BE `STT_MICROPHONE_PREROLL_BYTES` = 64,000 bytes / 2秒 | 確認前の語頭保持に利用し、上限を維持する |
| STT | 最大30秒/発話、待機3件・最大2,880,000 bytes、open capture最大4件 | 既存上限を維持し、超過を通知する |
| preview / 準備 | 有効信号800ms、preview最大3回。静音300msでSTT準備 | 再利用する。VAD終了・入力確定とは区別する |
| FE取得 | mono、echoCancellation=true、noiseSuppression=true | FEに維持する |
| 既存wire | Core 1.1、private topic `digital-souls.livekit-transport.v1` | 下記の新契約へ一括更新する |

設定の出典は[FE区間検出](../frontend/src/lib/audio/utterance-detector.ts)、
[補助判定](../frontend/src/lib/audio/short-speech-evidence.ts)、
[idle reset](../frontend/src/lib/audio/idle-vad-reset.ts)、
[BE capture](../backend/app/livekit_transport/microphone_bridge.py)。
M1では既存4ファイルのunit testが成功し、CPU ONNX Runtime 1.27.0とWasmtime 36.0.0で同じ資産の初期化・無音frame処理を確認した。
これはモデルの移設可能性の確認であり、音声品質比較の代用ではない。

## 新しい責務とwire契約

Core protocolを`2.0`へ更新する。LiveKit private frameはtopicを`digital-souls.livekit-transport.v2`へ分離し、
private schemaとbootstrapで新しいtransport契約を検証する。Coreとprivateのversionは別の値として検証する。
旧1.1 clientはbootstrapで拒否し、旧Sessionを引き継がない。旧WebSocket baselineは対象外。

| イベント・状態 | 決定者／方向 | 必要な対応 |
|---|---|---|
| 正式speech_started / speech_stopped | BE → FE | utterance_id、input_generation、media sample位置、BE検知時刻をBEが発行する。clientからの送信は拒否 |
| audio_input_open_requested | FE → BE | device事実として新しいtrack_sidとrequest event IDを通知する。世代番号はFEが採番しない |
| audio_input_opened | BE → FE | request ID、認可track_sid、BE採番input_generationを返す。音声採用開始のACK |
| mute / focus抑止 | FEの即時device抑止 → BEの入力ゲート | 端末で先に抑止し、BEは未終了captureと旧trackの入力を閉じる |
| text submit | FEの即時再生停止 → BEの入力優先・取消 | 未確定音声を無効化し、後続音声は新しい入力境界から受ける |
| 相槌／take-turn／応答・取消 | BE → FE | 既存判定を再利用。FEの実再生通知から停止対象responseをBEが対応づける |
| 再生開始／範囲／停止／終了／失敗 | FE → BE | deviceの事実として継続。生成終了・配信量で補完しない |

イベント名・fieldは上記を実装基準とし、型はJSON Schemaから生成する。
数値時刻のclock domainを必ず明示し、既存client時刻fieldへBE時刻を偽装しない。
Session、音声入力世代、utterance、response、playbackの識別を混同しない。
BEのID発行は再送した同一requestに対して冪等とし、stale requestで現在の入力ゲートを上書きしない。

### 入力境界とmedia／control順序逆転

一時抑止・mute・text優先・再接続は旧trackを採用対象から外す。
音声を再開するとき、FEは既存MediaStreamの取得権限を保ってmicrophone trackを再publishし、新しいSIDを通知する。
BEは現在の認証済みParticipantに所属する新しいmicrophone SIDだけを認可し、入力世代を進めてACKする。
FEはACK後にdeviceの送信を有効にする。新trackがcontrolより先に届いても未認可音声を採用しない。
旧trackのframe・遅延通知は世代／SIDで破棄し、時間経過による「おそらく古い音声はなくなった」を再開条件にしない。
再開待機は5秒を上限とし、timeoutは明示エラー・入力抑止を維持する。
focus解除がmanual muteやthread switch muteを解除しない条件は変えない。

capture・preroll・VAD状態は音声入力世代に所属する。focus時は未終了captureだけを破棄し、終了済み発話は処理を継続する。
同一Sessionのtext submitは先行未確定STTを含め無効化する。確定・保存済み履歴は消さない。
各処理完了は自分の世代と対象の有効性を検証し、古いtaskの終了で後続処理の状態を消さない。

## PCM処理・欠落・準備完了

LiveKit Python 1.1.16のAudioStreamは16kHz mono PCMを返すが、frame単位の送信側RTP timestampは返さない。
[SDKの公開AudioStream](https://docs.livekit.io/reference/python/livekit/rtc/audio_stream.html)と導入済みコードを照合した。
到着時計をmedia時計と扱わず、frameのsample数から連続した受信media位置を管理する。

- 公開FrameProcessor拡張点でqueue投入前にsample位置を付け、AudioStreamの待機上限を160個の10ms frameにする。上限で失った区間は次frameの位置差として検出する。
- VAD推論はイベントループ外のCPU workerで直列化し、Sessionごとの待機を16個の96ms frameまでに制限する。超過した発話は破棄・通知し、無制限に遅れて処理しない。
- transportの欠落はtrack統計のconcealed_samples等、観測できる復元不能区間とローカルsample欠落で扱う。jitter・一時的packet lossだけで発話破棄を決めない。
- concealed sampleの非無音分が連続観測区間で80ms以上増加した場合を破棄対象とする。統計は100ms間隔、timeout 1秒で確認し、発話確定前に最新値を照合する。低頻度の統計では連続性を断定せず、観測窓の欠落として記録する。
- queue由来の明示的sample欠落、track終了／交換中の未終了発話、30秒を超えた発話は対象発話を破棄する。復元不能音声は話し直し案内へつなぐ。
- 欠落統計が利用不能なら、その制約を明示して対象発話を保留し、上限内に確認できなければ破棄する。未観測を欠落なし・品質合格へ読み替えない。
- 長い静音でもPCMを蓄積し続けず、prerollと証拠窓の上限に収める。idle resetはsample数で進める。
- 音声入力開始ACKは認可trackの最初の有効受信統計を確認してから返す。BE準備は4秒で打ち切り、FEの5秒ACK待ちに収める。未観測のまま利用者発話を開始させず、準備失敗はtrack利用不可として拒否する。
- モデルhash、WASM、I/O、CPU推論の初期化を音声Sessionの利用開始前に確認する。失敗は音声利用不可として通知し、FE判定へ戻さない。
- 実行中のVAD推論失敗は未終了発話を破棄して通知し、reset後に700ms以上の静音を確認してから新発話を採用する。失敗した発話の語尾を新発話へ変換しない。
- resetにも失敗した場合、または認可trackのreaderが終了・失敗した場合は、その入力認可を破棄する。無音中も `error_code=audio_input_unavailable`、`user_state=muted` を通知する。終了済み発話・text・回答再生をreaderの失敗だけで取り消さない。
- 入力停止エラーは `track_sid / input_generation / input_revision` を必須とする。FEは現在の認可に一致する停止だけを適用し、手動再開では新SIDのACKを取り直す。開始ACK直後で送信完了待ちの間も、停止通知を受けた入力を有効化しない。
- 入力開始時のreset失敗は `audio_input_rejected(reason=vad_unavailable)` で返す。暗黙のFE VAD復帰や無限の自動再試行は行わない。

上限は既存のcapture・preroll・FE backlogを基準に設定する。80msは約1 VAD frameより短い欠落を区別する初期基準であり、
M2の固定音声・欠落試験で根拠を検証する。値を変更する場合は仕様の破棄／話し直し動作を維持し、変更根拠と回帰を記録する。
共有Whisperの再ロード・新VADサービスの起動は不要。ONNX RuntimeはCPUで1 intra-op / 1 inter-op threadを使用する。

## 移設前後の測定条件

既存`scripts/voice_quality/run_pilot.py`、既存fixture・集計・clock相関を使用する。独自の評価基盤は新設しない。
移設前commitは冒頭の値、移設後は受入対象の最終コミットを固定する。
双方を同一のdev／test専用Profile、同一の推論設定・モデルdigest、同一host・network条件で測る。
推論設定とモデルdigestは最初の測定開始前にrun manifestへ記録し、後側で一致を検証する。秘密値はartifactへ出さない。
#329の比較とは分け、通常応答・相槌・take-turnの比較TTSはVOICEVOXを固定する。現在のIrodori選択契約の回帰は別に確認する。

| 対象 | 事前固定する条件 |
|---|---|
| 通常応答 | 既存speech fixture・hash・正解境界。warm-up 5回除外、独立session／conversationで100試行 |
| 相槌／take-turn | 既存voice-quality-v2のラベル・音声・提示順を固定し、それぞれ100試行以上。失敗を別成功試行で置き換えない |
| 文中の間 | 既存pause cohort、短発話・雑音・連続背景音のfixtureを使用 |
| 再接続 | 既存の実network障害注入と全100試行を保持。無応答・初回失敗を分母から除外しない |
| 集計 | Hyndman-Fan Type 7。p50は診断、p95は指示書の移設前p95 + max(10%,50ms)で追加回帰判定 |
| 起点 | 固定fixtureの利用者発話境界を使用。BE検知時刻・FE通知受信時刻を代入しない |
| 時計 | 既存control probeのclient/server相関と誤差、media sample位置を使用。実マイクは推定可能範囲と測定不能を区別 |
| 除外 | warm-upと事前定義した対象外のみ。失敗・欠測・未知結果は理由と元の分母を保持 |

#150の元の目標値・WebSocket比較条件、#350の既存未達は保持する。
数値閾値を通っても誤停止・見逃し・二重入力・stale提示の回帰を許容しない。
局所unit/moduleと固定音声の比較、実サービス・ブラウザ、人の実マイク・聴感確認は別の証跡にする。
M6では人の確認が未実施なら未完了として残す。

### take-turn集計の起点と誤差

既存のラベル付き測定ハーネスは、take-turn manifestに
`latency_origin=scheduled_fixture_speech_start` と `input_authority` を記録する。
現行BE版は既定値の `backend`、旧FE版を同じハーネスで比較する場合は
`VOICE_QUALITY_INPUT_AUTHORITY=frontend` を指定し、実行するFE／BEの版と一致させる。
メタデータを持たない過去artifactは従来のFE VAD起点として読み、同じ起点の比較とは扱わない。

`scripts/voice_quality/report_take_turn.py` はfixtureのspeechStart上下限と、
同一ブラウザ時計の実停止・取消確認を使う。判定受信traceの整数ms切り捨てには上限1msを加える。
主指標のp50／p95と既存の合否には遅延の上限を採用し、
`fixture_latency_bounds` にType 7の上下限・測定数・欠測数を残す。
BEのserver時計はclient時計から減算せず、server取消処理の内訳にのみ使う。

BE発話のsession／utterance／responseとtrack・入力世代・sample境界を照合し、
通知の受信時刻はfixtureとの因果関係の確認に限る。
相関不足、観測overflow、重複・曖昧な停止、起点と終点の重なりは理由付き欠測とする。
初期化やcleanupに失敗した試行も全体の分母に残す。
集計schemaとvalidatorは起点・上下限・分母・主指標の一致を検証する。
これらは測定手順の定義であり、100試行の前後比較や実マイク受入を完了した証跡ではない。

## 段階適用と切り戻し

M2はtransport非依存のVAD・区間処理と局所試験、M3は新schema・BE/Core、M4はFE入出力clientと新契約へ接続する。
M2〜M4の途中Epicを配備可能と扱わない。旧1.1と新2.0の互換期間を設けるための二重判断器は作らない。
M5で既存計測と横断回帰を整合し、M6で一括切替・切り戻しを実測する。

今回の音声判断移設では保存済み会話履歴schemaの変更を要求しない。
切替は会話終了 → 対応するFE／BEの版へ更新 → ブラウザ再読み込み → 新Session開始。
切り戻しも会話を終了してFE／BEをそろえて旧版へ戻し、保存済み履歴は保持する。
具体的な実行・確認コマンドは[更新・切り戻し手順](voice-backend-rollout.md)を参照する。
手順の記載と、通しの実切替検証の完了は区別する。


## M3／M4接続中の補足契約

- input_revisionはFEの明示操作・入力再開要求の順序番号。BEのinput_generationとは別で、正式発話や入力世代をFEに採番させない。
- mute／resume／focus／text submit／openに順序番号を付ける。古い抑止・openは現在のゲートを上書きしない。text本文は古い順序番号だけを理由に配送ACK済みとして捨てず、既存の入力結果契約へ渡す。
- audio_input_openedは元のrequest event ID、track SID、revision、BE generationを照合する。拒否はaudio_input_rejected、5秒で確認できなければ入力を有効にせず再操作を案内する。
- Speechイベントはuserの発話を表すBE通知。sample位置のstart ≤ active_end ≤ detectedと16kHzを検証する。検知時刻はserver_monotonicで、FE通知受信時計を発話起点へ代入しない。
- 欠落確認ではOpusの48kHz受信統計と、16kHz VAD用PCMのsampleを区別する。現在の実装はaudio/opus・clock_rate=48000と必要なcounterの存在を確認し、それ以外は確認不能として扱う。実サービスでのcodec・counter更新頻度・観測範囲の検証は未了。
- 非無音concealmentはconcealed_samplesからsilent_concealed_samplesを除く。packet lossやjitterの値だけでは破棄しない。意味の根拠は[WebRTC統計仕様](https://www.w3.org/TR/webrtc-stats/#dom-rtcinboundrtpstreamstats-concealedsamples)を参照する。
- 統計のrequest後に届いたPCMまで確認済みにはしない。確認不能は1秒の上限まで保留し、確認できなければSTTへ渡さず、音声の話し直しを案内する。
- 上記は接続実装の契約補足。新しい局所試験・本番ビルドの成功だけで、既存suiteの移行や実サービス受入を完了としない。

### BE VAD境界と実STT入力のsample対応

BE版のVAD境界reportは、開始通知を発行したframeのtrack sample位置とBridgeの受信連番を対応付ける。
終了時には最初のawaitより前に対応を固定し、連続したcapture範囲・preroll・STTのprefix除去を検証する。
世代をまたいで継続する受信連番と、新しいtrackのsample位置を同一視しない。
対応できない場合は位置を補完せず、数値traceのmedia spanを無効とする。

固定音声への対応には既存の二つの独立PCM anchorとv4端部照合の両方を要求する。
相関・競合peak・sample数を再検証し、anchor間の位置差が32 sampleを超える場合は欠測とする。
上下限は確認したoffsetに既存の32 sample許容幅を加えた範囲であり、
物理時計の誤差保証や音声内部全体の連続性保証ではない。
VADの開始・検知終了位置から固定音声境界までを16kHzのmedia軸で集計し、server時計とbrowser時計は減算しない。

誤分割は正式BE境界から独立して件数を保持する。複数のSTT入力を結合して一発話のPCM証跡へ変換しない。
境界offsetが欠測でも、確認済みの誤分割を分母・件数から除外しない。
100試行条件、全件coverage、cleanup条件は維持する。人の実マイク・聴感受入は別途必要である。
