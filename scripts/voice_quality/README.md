# 音声品質の診断コマンド

これらは#150の診断と制御測定の実行入口。通常100試行の収集成功だけで、各cohortの全stack受け入れや実声dogfoodの完了とは判断しない。

## 独立data rootでの実音声pilot

専用worktreeのrootから、既存の推論サービスの設定ファイルを明示して実行する。

```bash
backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --run-id residency-01 \
  --inference-env /home/asa/dev/digital-souls/backend/.env \
  --trials 3 \
  --disable-thinking
```

`--run-id`は毎回新しくする。`frontend/test-results/livekit-quality/runs/<run-id>/`が存在すれば拒否し、失敗した試行のDB・traceも上書きしない。外部推論サービスの設定だけを指定ファイルから読み、dogfoodのdata rootは引き継がない。LiveKitの鍵は既定では`infra/livekit/.env`から読む。test用LiveKitは`127.0.0.1:7880`、テストのFrontend／orchestratorは5173／4174を使用するため、起動前に競合を確認する。

`--disable-thinking`はpilot内だけで`think:false`を指定する実験条件である。人格・履歴・記憶を削らないが、元設定と同じ応答品質が保証されたという意味ではない。指定しなければ既存の生成optionsを維持する。共有Ollama、Whisper、VOICEVOXの再起動や設定変更は行わない。

runnerは準備1回と指定回数の測定を行い、実際のBrowser→LiveKit→Whisper→LLM→VOICEVOX経路でtranscript一致・応答完了・session終了を確認する。応答sourceとreceive／decode／playbackの対応が未実装の場合、理由付き欠測を残す。このpilotをcontrolled受入へ変更して扱わない。

## 音声源の正解境界を直接観測するpilot

`--scheduled-fixture`を追加すると、マイクの準備・publish完了後に固定WAVのPCMをAudioWorkletからMediaStreamへ流す。開始前にfixtureを消費しない。各境界のPCMを出すprocess呼び出しより前にmainから送ったping時刻と、通知をmainが受け取った時刻で境界を囲む。clock名はclient側の`performance.now()`で統一する。正解境界をVAD判定や`getUserMedia`完了時刻から作らない。

```bash
backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --run-id source-clock-01 \
  --inference-env /home/asa/dev/digital-souls/backend/.env \
  --trials 3 --disable-thinking --scheduled-fixture
```

manifestの`fixture_clock_bounds`にはsource開始、正解発話開始、正解発話末尾の`lowerMs`／`upperMs`と元sample位置を保存する。上下限の幅が20msを超えた場合や欠測・逆転があれば試行は失敗する。遅延を単一値へ変換する必要がある場合は、末尾の下限を起点として遅延を過小評価しない。境界後に高頻度pingを停止する。

これは合成PCMの生成時刻の観測であり、物理マイク・スピーカーの時刻ではない。通常のfake microphoneとは入力供給方法が異なるため、凍結済みWebSocket baselineと同じ方法で計測したとは扱わない。WAVのhashとsample列は維持し、診断方法をmanifestへ明示する。応答音声のsource相関や全cohortの受け入れを完了したという意味ではない。

## 共有推論環境の数値観測

`inference-runtime.jsonl`へOllamaのロード済みモデル状態を約0.5秒おき、GPU使用量を約5秒おきに保存する。APIやコマンドの処理時間により間隔は延びる。観測失敗は欠測理由で残し、0使用量へ補完しない。

- 保存対象: ロード済みモデル数、対象モデルの有無、context token数、モデルのRAM／VRAM bytes、GPU使用率と使用／総memory bytes。
- 保存しない情報: model名・digest、endpoint、秘密値、prompt、transcript、任意のprovider応答文字列。
- GPUはhost全体の値であり、この会話だけの使用量ではない。BackendプロセスのRSS、transport別帯域、packet lossの代用にはしない。
- 時刻はobserver自身のmonotonic clock。BackendコンテナのtraceやBrowserのclockと同期したとは仮定しない。API呼び出しの開始・完了を両方記録する。
- 約0.5秒の離散観測なので、サンプル間に発生したcontext切り替えやqueue待ちを直接証明できない。

Ollamaの`load_duration`はprovider報告値として扱う。v0.32.5の[ChatHandlerとschedulerの実装](https://github.com/ollama/ollama/blob/v0.32.5/server/routes.go)では、handler開始からschedulerがrunnerを返すまでの時間が含まれる。重みを読み直した時間や純粋なqueue待ちへ分離した数値ではない。

## 正解ラベル付きfixture

[v1](../../frontend/playwright/fixtures/voice-quality-v1/README.md)と[v2](../../frontend/playwright/fixtures/voice-quality-v2/README.md)に、独立したラベル、音声生成クレジット、WAV hash、正解境界、展開・診断手順を記載する。`fixtures.py`と`stt_turn_diagnostic.py`は`--cases`で対応する固定ラベル定義を選択できる。


## 同一sessionの応答track切替診断

`--continuous-turns 3 --scheduled-fixture`を指定すると、同じsession／conversationとマイクstreamを維持して固定音声を3回供給する。最初のマイクON後はマイクを操作せず、各応答のtrack名のresponse ID、一意な応答ID、transcript一致、応答完了、最後の明示終了を確認する。

```bash
backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --run-id response-tracks-session-02 \
  --inference-env /home/asa/dev/digital-souls/backend/.env \
  --disable-thinking --scheduled-fixture --continuous-turns 3
```

このモードのmanifest scopeは`continuous_response_track_diagnostic`で、warm-up除外や独立100試行を実施するものではない。`--trials`の独立試行数はこのモードでは使用しない。固定音声全体の供給と、各応答の全sampleがbrowser出力時計を通過したことを確認して次へ進む。固定時間の待機をplayout完了の代用にしない。fixtureの途中でreplayを要求しても巻き戻さず、完了後の明示要求だけを受理する。通常pilotの起動では環境から残ったcontinuous指定を引き継がない。

trackの応答IDだけでは相関を確定しない。同じ受信packetを状態付き復号してworkletへ供給し、出力時計通過を確認してからreceive／decode／first playbackを対応付ける。元source PCMのcodec lookaheadやlogical segment内の厳密なsample位置については別の検証が必要である。診断中の会話履歴は専用runのdata rootに残し、dogfoodは使用しない。


## native AudioSourceと独立decode・出力の診断

```bash
backend/.venv/bin/python scripts/voice_quality/probe_native_audio_source.py \
  --output frontend/test-results/media-boundaries/native-source-shared-decoder-01.json
```

出力先は毎回新しい名前を指定する。専用の一時Roomを`127.0.0.1:7880`へ作り、Python SDKから48kHz monoの合成440Hz音を送信し、Chromiumで実WebRTC受信する。推論サービス・会話DB・dogfoodを使用しない。終了時は自分が作ったRoom、AudioSource、browser、localhostの静的診断ページ用serverを解放する。鍵とRoom用JWTはファイルやstdoutへ保存しない。

標準buffer（1,000ms）と直接入力（0ms）を比較し、購読後1秒の入力前、20ms入力後、追加1秒入力後を観測する。直接入力は10msずつ実時間で供給する。[RFC 2198](https://www.rfc-editor.org/rfc/rfc2198.html)に従って受信REDからprimary payloadを取り出し、[WebCodecs Opus](https://www.w3.org/TR/webcodecs-opus-codec-registration/)として独立decodeする。通常会話の受信観測と同じ`OpusPacketDecoder`を使い、途中でflushせずに各packetの960sample出力を待って入力番号を対応付け、PCMをAudioWorkletへ渡す。同じsnapshotまでの全packetを別decoderで一括復号し、PCM全サンプルの差分が0であることも検証する。比較用に途中flushするdecoderの波形差分も数値で記録する。PCM本体は診断JSONへ保存しない。decoderの`AudioData.timestamp`は再構成され得るため、入力packetの識別子に使わない。

workerと画面は10回の往復messageで時計offsetの上下限を求める。このChromium診断では100usの時計丸めを前提としてoffsetの両端に0.2msの余裕を残し、幅が1msを超えた場合や矛盾した場合は失敗する。これは全browserの時計精度を保証する方法ではない。`timeOrigin`だけの換算値は比較診断用に残すが、遅延へ採用しない。出力frame位置は`getOutputTimestamp()`でbrowser出力時計へ写し、最後の観測で出力時計が全サンプルの末尾を通過したことを確認する。物理スピーカーの音響時刻は測らない。

検証器は直接入力の0→1→51 packet、同数のdecode・render・完了、RTP timestamp／sequenceの連続性、packet相関、各960サンプルの出力、時計の順序を必須にする。別のsource PCM sample位置との一致、通常会話の明示PCM再生、loss回復、実音声全般の音質、TTFA・underrunの受け入れ完了は示さない。最初の20ms後の200ms停止は診断で意図的に入れるため、playback continuityの合格試行として集計しない。


## 準備5回＋独立100試行

```bash
backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --run-id controlled-01 \
  --inference-env /home/asa/dev/digital-souls/backend/.env \
  --controlled --scheduled-fixture --disable-thinking
```

`--controlled`は`--trials`と排他的で、少数pilotの環境変数を除去して5 warm-up＋独立100 sessionを選ぶ。固定PCM時計なし、連続session診断との混在は拒否する。上の例は`think:false`の実験条件を含み、元の生成optionsと同じ品質の証明ではない。RAGなしのintegration-voiceで、personaと会話の初期状態を毎試行照合する。

完了したrunは`python -m app.livekit_pilot_report --scope controlled`へmanifest・trace・profile reportを指定して集計する。出力sample数、出力時計、receive／decode trace、固定音声の境界、独立ID、空の会話・記憶状態を検証する。品質の合否はさらに`voice_metrics.evaluate_artifact`で凍結済みWebSocket baselineと比較し、未測定のcohortを合格へ補完しない。


## STTの待機後遅延

```bash
backend/.venv/bin/python scripts/voice_quality/probe_stt_idle.py \
  --output frontend/test-results/media-boundaries/stt-idle-new.json
```

固定fixtureの同じPCMに対して待機間隔を変え、100msの無音を先に認識する準備も比較する。音声・本文は保存せず、一致判定と時間だけを残す。既存出力は上書きしない。無音処理の結果は会話へ送らず、モデル・サービス設定は変更しない。先頭の追加待機0秒は、診断開始前のidle時間を表さない。前景の実会話・品質測定と並行して実行しない。


## CPU・メモリ・GPU・音声RTPの診断

`run_pilot.py`は`inference-runtime.jsonl`へ、run reportが所有するBackendコンテナのCPU累積時間・メモリ使用量を定期記録する。取得先は`integration-voice`、`test`環境、当該runのdata rootと一致する場合に限る。コンテナIDや例外本文は数値ファイルへ出さない。GPUは共有host全体の値を低頻度で記録する。

集計時に、同じrunの`--resource-observations <run-root>/inference-runtime.jsonl`を`python -m app.livekit_pilot_report`へ追加する。CPUは連続して取得できたsample間のCPU時間増分／観測時間の平均で、1 core占有を100%とする。メモリはsampleのcontainer charged bytesの最大値であり、RSSやsample間の瞬間peakとは異なる。GPUは各sampleの最大device使用率と全device使用量合計の最大値を保存する。観測件数、CPU区間長、最大interval、取得失敗理由も`resources.collection`へ残す。

正常応答の全PCM再生後、マイクの送信RTPと応答trackの受信RTPを`getStats()`から取得する。送信は同じsenderの前回応答snapshotとの差分、受信は応答専用trackの累積値とする。stats ID、SSRC、IP、音声本文は出力しない。同じsenderの巻戻り、複数音声stream、未取得値、2秒のtimeoutは理由付き欠測にする。欠測を0byte／loss 0件で補わない。

通信量はRTP payloadであり、RTP header・padding・UDP/IP・DataChannel・signaling全体のwire量ではない。損失率の範囲はbrowserが観測した下り応答trackで、分母は受信packet数＋各trackの正のloss数。負のloss値を別trackの損失から相殺せず、その発生件数を残す。[W3C WebRTC stats](https://www.w3.org/TR/webrtc-stats/#dom-rtcreceivedrtpstreamstats-packetslost)では累積推定lossが負になる場合も定義されている。上りlossや疎通障害中の継続サンプリングは、この正常再生完了時のsnapshotだけでは証明しない。

`network.collection`には送受信・loss各方向の取得試行数と欠測理由を残す。通常artifactではwarm-upを除外する。同一session診断にも各応答のsnapshotを保存し、送信累積値の二重加算を確認できる。


## 追加操作回数

scheduled fixtureのpilot・controlledと同一session診断は、開始ボタン完了後にdocumentのclick activation observerを開始する。Enter等のbutton activationもclickとして数える。対象の文言・本文は保存せず、開始・snapshot・activationの単調時計だけをraw manifestへ保存する。明示終了とcleanupはsnapshot後に行う。

`livekit_pilot_report`は発話開始前から全sample再生完了までを覆う観測を検証し、準備試行を除外して`manual_operations`へ集計する。旧manifestや実PCM境界がないpilotの欠測を0に補わない。同一sessionのsnapshotは累積値なので、sessionの操作総数には最後のsnapshotを用いる。通常の音声fixture送出はUI activationではなく、マイク入力を再現する操作である。


VAD候補をアプリへ反映する前に比較する場合、`measure-utterance-detector.mjs`では確率JSON・出力JSONの後に、`measure-utterance-background.mjs`では出力JSONの後に、候補のTypeScript sourceを指定できる。省略時は現在のアプリsourceを使う。両診断は既存出力を上書きせず、source・modelのhashを結果へ保存する。非発声音の診断は各frameの実Silero確率もraw結果へ残す。比較では無音など同じPCMの反復をhashで潰さず、元の試行順・種類・hashを対応付ける。


## 実応答の再生中にラベル付き音声を入れる

```bash
backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --run-id labeled-take-turn-new \
  --inference-env /home/asa/dev/digital-souls/backend/.env \
  --trials 4 --scheduled-fixture --disable-thinking \
  --interruption-cohort take_turn
```

`--interruption-cohort`は`take_turn`または`backchannel`で、固定v2 manifestの対象cohortから先頭の`--trials`件（1〜100件）を選ぶ。展開済み音声が必要。通常のcontrolled測定・同一session診断とは混在できない。初期音声への応答が実再生を始め、再生完了前であることを確認した後、同じマイクstreamのfixture sourceをラベル付き音声へ切り替える。外部サービス、VAD、STT、発話権判定、キャンセルは置換しない。

raw manifestは`labeled_livekit_interruption_diagnostic`として別scopeで保存する。判定が来なかった試行も削除せず、初期応答・再生重複・fixture／判定・キャンセル／継続の失敗段階を記録する。全件成功しなければPlaywrightは失敗する。cleanupの終了操作は観測後に行い、明示終了の失敗も残す。

診断時だけVADのframe確率・RMS・sample数・単調時計と候補／確定／終了イベントを観測する。上限は1024 frame・128イベントで、PCMや本文をこのportへ渡さない。これは待機後やframe開始位相による単独検出器診断との差を調べるための証拠である。正常応答用`livekit_pilot_report`へこのmanifestを渡して通常100試行として集計しない。全cohortの匿名aggregateと受け入れ評価の統合は別途必要。


## take-turn全試行の相関集計

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/voice_quality/report_take_turn.py \
  --manifest <run-root>/trial-manifest.json \
  --trace <run-root>/runtime-data/voice-metrics/controlled-trace.jsonl \
  --fixtures frontend/playwright/fixtures/voice-quality-v2/manifest.json \
  --output <新しい出力ファイル>.json
```

全件の記録が終わってから実行する。期待件数より少ない途中run、fixtureの順序・hash不一致、sessionの再利用は拒否する。出力は既存ファイルを上書きしない。各latencyは既存`voice-quality-artifact-v1.schema.json`内のmetric schemaで検証し、集計全体は`voice-quality-cohort-report-v1.schema.json`でも検証する。このcohort結果を通常応答100件のartifactとして扱わない。

音声投入時計が20msより不確かな試行は投入未検証として残す。見逃し率の有効投入分母とは別に期待数・記録数・投入未検証数を保持し、投入・終了・独立性のいずれかが未確認なら100件条件の合格を認めない。取得分のp95が閾値以内でも欠測があればlatencyの評価は不合格にする。正常cancelを通常応答の処理失敗へ加算しない。

local stopは元の発話開始時刻と旧responseでBrowser／traceを照合し、cancel ACKが欠測でも独立して計測する。serverのcancel時刻だけが残り、キャンセル通知が観測されないケースは成功cancelへ補完しない。欠測理由は固定の理由コードとし、本文やIDを出力しない。別callbackで取得するBrowser停止・確認時刻は、同じIDとclockで2ms以内の差だけを許す。


## 相槌の誤cancelと旧応答の全出力

`--interruption-cohort backchannel --scheduled-fixture --trials 100`で独立sessionの相槌cohortを実行する。割り込み前の旧応答について、初回packetの受信・復号・実出力観測を保存し、後の新応答の観測で上書きしない。

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/voice_quality/report_backchannel.py \
  --manifest frontend/test-results/livekit-quality/runs/RUN/trial-manifest.json \
  --trace frontend/test-results/livekit-quality/runs/RUN/runtime-data/voice-metrics/controlled-trace.jsonl \
  --fixtures frontend/playwright/fixtures/voice-quality-v2/manifest.json \
  --schema docs/schemas/voice-quality-artifact-v1.schema.json \
  --output docs/artifacts/backchannel-UNIQUE.json
```

集計は全件・ラベルhashと順序・独立session・投入時計を照合する。local stopとserver cancelは個別に数え、片方だけでも誤cancelとして扱う。通知がない試行でも、同じ旧応答の初回packet、source sample総数、連続RTP、全出力時計、対象音声終了までの再生継続を確認できなければ欠測にする。欠測は全試行の分母へ残し、観測・終了の全coverageと最低100件が揃うまで誤cancel率を合格にしない。

相槌と認識できなかった試行でも、旧音声の全出力継続を直接証明できれば「誤cancelなし」には数えられる。分類済み件数は別に報告し、この指標の合格をVADの未検出や冒頭／終了境界の合格へ広げない。誤cancelはこの品質指標のfailureであり、正常な割り込みcancelを通常応答の処理失敗へ混ぜない。出力のmetric schema・匿名性・raw入力と集計器／検証器のhashを確認し、既存artifactを上書きしない。


## cohort集計全体の検証

相槌とtake-turnのCLIは、個々の指標に加えて集計全体のscope・型・必須項目・固定の計測境界・指標名を検証する。schema参照はローカルのmetric schemaへ登録して解決し、外部のschema取得を行わない。出力にはcohort schemaのversionとhash、整合性検証器のhashを残す。

`cohort_report_validation.py`は、期待件数・記録件数・成功失敗の和、全metricの分母、欠測・除外理由の件数、分類済み／保留／未判定の和、ステータスと観測数、見逃し率とlatencyの合否を照合する。少数pilotを100件合格にする判定や、欠測を除いた分母での合格を拒否する。合否閾値と集計値の算出方法は既存の定義を維持する。


## 会話状態を変えない制御probeの実接続診断

```bash
backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --run-id control-probe-new \
  --inference-env /home/asa/dev/digital-souls/backend/.env \
  --control-probe --trials 3 --scheduled-fixture --disable-thinking
```

`--control-probe`の`--trials`は、1 session内のprobe回数（1〜10）を表す。通常の独立試行数ではない。制御測定100件・割り込み・連続会話の各モードとの混在は拒否する。実応答の再生中へ100ms間隔でprobeを送り、応答継続・全出力完了・明示終了を記録する。上の例は実験用think:false条件であり、通常設定の品質試験ではない。

private transportの`control_probe`と`control_probe_ack`はUUIDと世代を相関する。Backendは現在のparticipant・世代・利用可能phaseを確認し、世代・Core・再生状態を変更せず応答する。通常会話からは自動送信しない。Browserは最大1件を500msだけ保持し、publish完了を往復成功にしない。切断・再接続・世代変更で待機を終了し、古い応答・時計の逆転・期限後の応答は成功へ補完しない。

raw manifestには同一Browserの`performance.now()`で記録した送信・ack受信時刻、probe成否、元応答の出力証拠を残す。これは障害なしのcontrol経路診断であり、音声のnetwork回復や再接続100件の成功率・遅延を証明しない。障害runnerとの時計対応と、実音声frameの回復・重複出力の観測は別に必要である。


control probeを専用19880 bridgeへ接続する場合は`--fault-bridge`と専用キーの`--livekit-env`を指定する。runnerとPlaywrightは`integration-voice-fault`を明示選択し、通常モードでは継承されたfault選択を除去する。専用環境の起動・所有検証・readinessの境界は`infra/voice-quality/README.md`を参照する。通常100試行や割り込みモードへfault選択を混在させない。


### 専用bridgeの実切断診断

`run_pilot.py --fault-bridge --control-probe --network-fault --trials 3 --scheduled-fixture`
は、実応答の再生中に専用bridgeだけを2秒切断する1 sessionの診断である。
`--run-id`、`--inference-env`、専用キーを指す`--livekit-env`も通常どおり指定する。
専用Profile・container label・排他的bridge・loopback公開portの検査を通過する必要がある。
通常環境への継承フラグはrunnerが除去する。

PythonとBrowserの前後の時計sample、切断・復旧操作開始・link復旧・TCP疎通event、
切断中と復旧後のcontrol probe、受信packetの数値情報と出力時計通過済み区間をraw manifestへ保存する。
復旧操作開始からTCP疎通成功までを復旧時刻の範囲とし、その下限から保守的な回復時間を計算する。
復旧後に送ったcontrol要求と、復旧後に受信した非無音packetの実出力を要求し、
以前に受信済みのbufferはaudio回復として扱わない。観測overflowや欠測も残す。
診断clientは、途中のBrowserエラーでもPython側のnetwork復旧処理の終了を待つ。

重複観測は同じresponse・SSRC・RTP timestampのsample区間の重なりを検出する。
SSRC変更後の同じsource音声の再送などを含む全面的な重複再生判定は別途必要であり、
この単発診断の成功を再接続100試行の受け入れとして扱わない。


実切断診断は回復計測の10秒窓を閉じた後、同じsessionの次の発話を自動投入する。
その応答の全出力を`post_fault_followup`へ別記録する。後続発話の成功を
回復時間3,000msの判定へ置き換えず、元の回復判定が未達ならrunは失敗のままにする。
RTP欠落による応答中断の時刻・欠落packet数も`media_packet_losses`へ残す。

## 再接続の匿名集計

専用bridge診断の`trial-manifest.json`を指定する。保存された合否や`recovery`値は流用せず、
切断前後の時計較正、実切断の時刻・長さ、切断中の不通、制御往復、受信・復号・実出力の
記録から再計算する。受信済みbuffer、無音、復旧前に送った要求への遅着ACKは回復へ数えない。

```bash
node frontend/scripts/report-fault-recovery.mjs \
  --expected 1 --output /tmp/reconnect-diagnostic-report.json \
  frontend/test-results/livekit-quality/runs/network-fault-session-09/trial-manifest.json
```

これは1件の診断例で、合格にはならない。正式評価は`--expected 100`と独立100件のmanifestを
指定する。同じfixture・同じ実装commit、独立session／conversation、全試行の記録と終了、
時計・障害・出力証跡、次発話の成功を照合する。試行抜け、重複session、異なるfixture／commitの
混在は拒否する。失敗は分母に残し、10秒内に両経路が復旧した全試行のp95を計算する。
全失敗のp95はnullとし、99%・3,000ms・重複再生0を別々に判定する。
重複の観測範囲は同一response／SSRC／RTP timestampの出力sample区間であり、
送信元変更を跨いだsource PCM同一性の検証を代替しない。

`run_pilot.py`は起動前にGitの未コミット変更を拒否し、実装commitを診断manifestへ記録する。
既存のローカル依存環境を指す`backend/.venv`の未追跡symlinkだけは許可する。
この変更より前のrawは実装版を後から推測して補完せず、版情報の欠測として集計する。
共有推論サービスや依存runtimeの版・条件は別の環境証跡で照合する。

出力は`docs/schemas/voice-quality-reconnect-report-v1.schema.json`で検証し、ID・本文・
入力path・任意の例外文字列を含めない。既存出力は上書きしない。
終了コードは合格0、不合格report作成済み1、引数・証跡・schema等のエラー2である。
この集計器のテスト成功は、実再接続100件の受け入れ成功を意味しない。

再接続の新しい診断では`audio_availability_method=fresh_rtc_probe_and_followup`を記録する。
専用bridgeの復旧後に送った制御probeの応答を待ち、同世代の新しいnonceで200msの固定音を
Backendから要求する。通常の会話と共通のPCM送信器、RTC、packet復号器、renderer、出力時計を
通し、入力9,600＋codec末尾960＝10,560 sampleの出力通過と後始末を照合する。
診断音はCoreの応答や会話履歴へ登録しない。通常の次発話の成功も別途必須とする。
これは音声transportの利用可能時刻を測る暫定の境界であり、次発話のSTT／LLM／TTS時間を含まない。
旧応答の再開・buffer・無音を新しい音声復旧へ数えない。

送信機能は`DS_ENVIRONMENT_ID=test`、`DS_PROFILE=integration-voice-fault`、
`VOICE_MEASUREMENT_KIND=controlled_baseline`、専用localhost:19880の全条件を満たす場合だけ有効。
同時実行1件、1 session当たり8 nonce、診断全体10秒、準備待機8秒、後始末にも期限を設ける。
合否の起点は要求時刻ではなく障害回復時刻で、10秒・成功時p95 3,000msの基準は変更しない。
rawには本文を含めず、nonce・SID・世代と数値packet証跡を保存する。匿名reportには識別子を出さず、
計測方式だけを残す。旧方式との混在は拒否する。statusが成功でも全packet・相関が欠ければ失敗分母に残す。

状態同期の直前・直後には制御probeと音声probeの世代が異なることがある。再集計は音声と
同世代の制御往復を選び、制御と音声出力の遅い方を復旧時間とする。同世代の制御確認が音声要求の
後でも、音声診断完了までに成立した実往復なら照合できる。別世代の早いACKでは補完しない。

## 再接続の独立100 sessionを逐次測定する

専用LiveKitを`infra/voice-quality/compose.yaml`の`ds-voice-quality-fault` projectで起動し、
共有Ollama／Whisper／VOICEVOXが利用可能な状態で実行する。runnerは専用bridgeのラベル・
排他性・port・container IDを各試行前に再照合する。LiveKitの起動・終了は呼び出し側が管理する。

```bash
backend/.venv/bin/python scripts/voice_quality/run_reconnect_cohort.py \
  --cohort-id reconnect-controlled-a --sessions 100 \
  --inference-env /path/to/inference.env --livekit-env /path/to/fault-livekit.env \
  --disable-thinking
```

`--sessions`は独立session数であり、単独pilotの`--trials`（1 session中の制御probe数）とは異なる。
各試行に異なるrun ID・data rootを割り当て、固定scheduled fixtureを用いる。
`--disable-thinking`は明示指定した場合だけ適用する。この条件の結果は通常設定の人格・記憶品質を
証明しない。100未満も実行できるが、診断扱いで受け入れ合格にはならない。

計画と進捗は`frontend/test-results/livekit-quality/cohorts/<cohort-id>/plan.json`と
`execution.jsonl`へ保存する。Git変更や版の不一致、既存runの上書き、同時cohort実行を拒否する。
試行失敗も分母に残し、子processの終了・専用test data root・Profile・所有コンテナの実削除を
確認できた場合だけ次へ進む。削除を確認できない場合はcohortを停止し、途中の結果を保持する。
中止したい場合はcohortディレクトリに`stop-requested`ファイルを作ると、現在の試行終了後に停止する。
全件終了後にrawから再計算し、schema検証済み匿名`report.json`を作る。
終了コードは合格0、不合格report作成済み1、途中停止／未検証2。


### VAD不具合の小規模再現

ラベル付き診断では`--fixture-indices 51,56,72 --trials 3`のように、cohort内の1始まりの番号を指定できる。
これは10件以下の原因調査用であり、100試行の受け入れには使用できない。重複や範囲外は拒否し、
省略時は通常どおり先頭から全件を使用する。選択した番号列は生記録の`fixture_indices`へ保存する。
診断用番号を環境変数から次の正式測定へ引き継がない。

VAD診断は各frameの確率・RMS・補助VADの有声割合・スペクトル集中度／平坦度と、モデルのリセット時刻を記録する。
PCMや本文を診断portへ渡さない。frame・イベント・リセット記録には個別の上限とoverflowフラグを設け、
古い値を黙って破棄しない。これらの追加観測だけではVADの精度改善や受け入れを証明しない。


### 実ブラウザのVAD境界集計

`report_vad.py --manifest <trial-manifest.json> --fixtures frontend/playwright/fixtures/voice-quality-v2/manifest.json --output <匿名集計.json>`
で、固定ラベルと実ブラウザの検出器イベントを照合する。fixtureのSHA、試行の独立性、元sampleと時刻の因果境界を検査する。
冒頭／終了誤差は20ms以下の時計区間から上下限を求め、100msの閾値をまたぐ場合は保守的に違反へ数える。
最初の終了を使用し、後半の終了によって早い分割を隠さない。欠測を分母から外さず、試行数不足・終了未確認も合格にしない。

出力は`docs/schemas/voice-quality-vad-report-v1.schema.json`で許可した集計値のみとし、schema検証後に新規保存する。
終了コードは境界条件達成0、未達1、入力／schema／保存エラー2。既存の出力を上書きしない。
これは検出器の境界であり、STTに取り込まれたPCMの冒頭・末尾を証明したものではない。
`limits`のPCM取込確認・VAD全体受け入れはfalseのまま保持し、文中無音cohortの測定や実取込の証拠を別途要求する。


### 語頭候補の保留に対する回帰診断

Frontendで`node scripts/measure-vad-runtime-cohorts.mjs <新規出力.json> legacy <utterance-detector.ts> onset-regression`
を実行すると、実ブラウザで失敗したtake-turnの51・56・72番を4ms刻みの24位相、待機500／2,000／6,000msで測る。
通常の`phase-sweep`は全300音声×6位相を維持する。両方とも固定PCMと仮想時計によるオフライン診断であり、
LiveKitの実ブラウザ100試行には代用しない。


### 文中休止の実ブラウザ測定

`run_pilot.py --vad-cohort pause --trials 100 --scheduled-fixture`に新しいrun IDと推論設定を指定する。
1試行ごとに独立sessionで先行発話を行い、固定ラベルの2節音声と200／400／600msの休止を入力する。
fixtureを全て出した後、VAD frame処理まで待って確認／終了イベントを数える。相槌やtake-turnという意図ラベルは要求しない。
正式な誤分割率・境界誤差は`report_vad.py`で全100件を集計する。未確定・複数区間・終了失敗も保存し、分母から除かない。
他の診断モードとの混在と、600msを超える休止ラベルを拒否する。

再接続制御の診断では`sendCompletedAtMs`をSDK送信完了時刻として追加し、ackの受信時刻と区別する。
送信が完了しないままtimeoutした場合はnullのまま保存し、後から値を書き換えない。
専用test Profileのサーバー診断はprobe受信・世代拒否・ack送信開始／完了を固定stageと単調時計で記録する。
各時計の値を直接減算して片道遅延とせず、同じ時計内の段階差から滞留箇所を切り分ける。受け入れの往復成功条件は変更しない。


## cancel後の音声出力をgain後段で診断する

通常の音声試験driverは`observeStaleAudio`を設定し、応答ごとのgainとdestinationの間へ
passthrough AudioWorkletを追加する。音声本文を記録せず、非ゼロsample数・先頭と末尾frame・
出力時計・応答とsession／generationの対応だけを保持する。明示診断がない通常経路へ追加しない。
割り込みのraw manifestは`stale_audio`と`stale_audio_overflow`へこの観測を保存する。

renderer／gainを停止・切断しても監視nodeをdestinationへ残す。停止前にrender済みで出力待ちの
sampleを捨てず、finish通知と`getOutputTimestamp()`の末尾通過、同じ末尾に対応する
`performance.now()`の通過を待つ。音声graphを閉じるときも最大2秒までdrainを待ち、
未観測なら`audit_closed_before_drain`として残す。終了通知だけで提示済みやstale 0とはしない。
finish後に非ゼロ出力が届けば、先にdrain完了を通知していても欠測へ更新する。

cancel境界はBrowserの`response_cancelled`受信時刻で、観測には
`client_cancel_confirmed`を明記する。サーバーのcancel時刻からの全区間とは別であり、
時計対応を確認するまで同一指標へ転用しない。sampleがcancel境界をまたぐ場合は
`nonzeroSamplesAfterCancelLower/Upper`に上下限を残す。観測範囲が境界の前後を覆い、
欠測がなく、上限が0の場合だけ、この出力窓の非ゼロsampleを0と確認できる。
後から欠測が届く可能性があるため、最初のcomplete行だけを採用しない。

`receivedAfterCancelPackets/Samples`はcancel後にmainへ届いた復号済み旧packetを、
再生抑止のreturnより前で数える。受信境界は`decoded_packet_delivered`で、Backend生成数や
encoded packetの最初の到着数ではない。textの受信／DOM提示、匿名aggregateと受け入れ評価への
統合は別途必要。この診断の導入だけでは#150のstale presented 0を満たさない。


出力のcancel相関は、各区間を別timestampから外挿する方式から
`bracketing_output_timestamps`へ変更した。cancel直前の利用可能なoutput timestampのframeを下限、
cancel直後の最初の利用可能なtimestampのframeを上限にする。未来を指すtimestampは採用せず、
このChromium診断のperformance時計丸めとして両端0.2ms、frameには外側1 sampleの余裕を残す。
この余裕はstale件数の合否閾値ではなく、推定範囲を保守的に広げるための値である。

cancelは直前pollの観測時刻以後でなければ受理しない。poll済み区間は採用した下限frame以前なので、
後からcancelへ含める区間を欠落させない。上限anchorを待つ間に出力時計を通過した区間は上限付きで保留する。
前後anchor不足、監視窓の不足、timestampそのものの逆行は欠測のまま残す。
`firstOutputAtMs/lastOutputEndAtMs`は表示用の外挿値で、staleの成否には使用しない。


再生用AudioContextは`latencyHint: 0`で最小遅延を要求する。これは保証値ではない。
診断行の`outputContext`にはブラウザが返すsample rate、base latency、output latencyを保存し、
未対応や非有限・負の値はnullとする。要求値から実遅延を補完せず、staleと再生欠落は従来どおり実出力で判定する。

### 旧応答テキストの受信とDOM追加

実ブラウザdriverの`stale_text`は、検証済みCoreイベントを重複除外前に観測し、
旧responseのdelta受信件数／文字数と重複件数を保存する。cancel後も受信を数え、
Controllerが破棄した受信を未受信と扱わない。文字数の単位はUTF-16 code unitである。

応答中の本文DOMへresponse IDを付け、MutationObserverで追加・置換を別に観測する。
cancel受信時には保留中のDOM観測を先に確定する。同じ長さの置換、除去後の再提示も数え、
共通prefix以後を新たな提示として保守的に扱う。本文は同一ページ内の比較だけに使い、
snapshotには件数と文字数だけを出す。重複DOM・本文上限・応答数上限は欠測またはoverflowへ残す。

現在のscopeは`live_and_history_response_dom`である。保存時に発行されたturn IDを
`response_started.history_turn_id`で伝え、履歴のassistant本文DOMを対応付ける。
履歴は`rows[].history`へ独立に記録し、ライブ表示と同じ文章の再表示も数える。
IDの欠測・変更・再利用、重複DOM、履歴未表示、監視区間不足は0件の証明にしない。
ライブ表示から履歴へ同じ接続済み本文nodeを維持した場合だけ、そのnodeで観測済みの共通prefixを
継続表示として引き継ぐ。`retainedLiveDomTransitions`と`retainedLiveDomCharacters`で遷移件数と文字数を記録する。
同じnodeでも一度除去した場合、別nodeへ作り直した場合、別応答の場合は引き継がない。
履歴への更新でprefix以後に追加・置換された文字は引き続き新規提示として数える。
本文とIDを匿名aggregateへ転記せず、履歴の文字数・上下限・欠測理由だけを集計する。
これはDOMへの反映でありOSの画面描画完了は証明しない。生観測のcancel前後は
`client_cancel_received`と明記し、正式集計では保持した各変更区間をサーバーcancelの因果上下限へ再評価する。
単に追加文字数が0でも、開始・cancel・DOMの観測、監視終了がそろわなければ完全な観測とはしない。

割り込み試行は終了前の`evidence`を保持し、終了後の`cleanup_observation`へ最終audio行と
停止済みtext観測を追加する。take-turnでは旧応答のaudio graph closedも確認し、
閉鎖を確認できない場合は`audit_cleanup_failed`として試行を失敗にする。


### サーバーcancel成立の時計境界

Coreはcancel状態の書き換え直前・直後の単調時計を`terminal_state_bounds_ns`として内部で保持し、
永続化を待った後も同じ値をLiveKit計測へ渡す。traceの`cancel_state_lower/upper`はこの2点であり、
既存のcancel通知送信時刻や、cancel処理から戻った時刻を成立時刻へ置き換えない。
この内部metadataを公開Coreイベントの本文へ追加しない。

ラベル付き実ブラウザ診断は`observe_clock: true`付きprivate control probeを直列に送る。
通常の復旧判定用probeとはpendingを分け、現在の主体・接続・世代だけに応答する。
ackはサーバーの受信・送信直前の単調時計をmicrosecondで返す。片方のみ、不正な整数、逆行は拒否する。
ブラウザの送受信時刻と別フィールドで保存し、最大2048回、overflowと終了状態を明示する。

`server_clock.py`の`bound_transition`は、サーバーで遷移前に処理したprobeのclient送信を下限、
遷移後に処理したprobeのclient受信を上限へ使う。両時計の原点・速度の一致、一定offset、
片道遅延=RTT/2を仮定しない。serverのmicrosecond切捨ては外側1us、clientの丸めは外側0.2msへ含める。
前後probe不足は`server_transition_not_bracketed`、矛盾する順序はエラーとする。
この境界だけではstale 0は証明できない。音声出力とDOMの履歴を同じ境界で再集計する必要がある。

### サーバー成立区間への出力履歴の再適用

`StaleAudioObservation.outputArchive`は、gain後段workletの非ゼロsample区間と
`getOutputTimestamp()`の各実観測を順序付きで残す。通常は直近4秒、cancel受信後は切り詰め位置を固定し、
最大4096記録とする。PCMは保持しない。元の欠測、時計逆行、overflow、閉鎖時刻を保存し、
切り詰められた過去や閉鎖より先の境界を補完しない。

`replayPostGainOutput`は別の計測器へ観測を再適用し、指定したcancel下限の直前に境界を挿入する。
確定済み区間を捨てた計測器の時刻制約を緩めない。前後anchor・区間連続性・finish・drainの検証は従来と同じである。

`decoded_receipts`はaudio graphの有無と独立し、復号済みpacket callbackの入口でsample数と時刻を残す。
graph準備前や切断後の受信も対象とし、media observerの閉鎖で最終記録を出す。閉鎖後のcallbackは欠測扱いとする。
直近4秒の履歴、切捨てた件数・sample数、全受信の件数・sample数を照合し、cancel後は履歴を切り詰めない。

textの`changes`にはdelta受信の時刻と、DOM追加の前回観測〜今回観測の区間を残す。
MutationObserverのcallbackが遅れた場合に、DOM変更をcallback時刻一点へ確定しない。
各応答最大4096件とし、本文は保存しない。追加・置換の件数と文字数は履歴の合計と一致することを要求する。

Frontendの`scripts/replay-stale-observations.ts`は、traceとの応答・session対応とserver時計の前後probeを
照合したtask内入力から、音声・復号受信・応答中DOMを同じ境界で再計算する診断CLIである。
各出来事の時刻区間全体がcancel上限以後なら下限に数え、cancel下限以後に起きた可能性があれば上限に数える。
曖昧な区間をstale0へ確定しない。入力・出力はtask内に0600で保存し、既存出力を上書きしない。
この個別窓の結果は、保存履歴の再表示・サーバー生成側・正式100試行と匿名schema検証を代替しない。


## サーバーcancel後のstale受信・提示を匿名集計する

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/voice_quality/report_stale_output.py \
  --manifest <run-root>/trial-manifest.json \
  --trace <run-root>/runtime-data/voice-metrics/controlled-trace.jsonl \
  --fixtures frontend/playwright/fixtures/voice-quality-v2/manifest.json \
  --output <新しい出力ファイル>.json
```

対象は終了済みのtake-turn全試行（1〜100件）。固定fixtureのhash・順序、独立session、
実投入の境界、明示終了、同じ旧responseのサーバーcancel状態遷移を照合する。
成功試行だけの抽出、途中run、任意のfixture選抜を受け付けない。
失敗・時計欠測・履歴欠落も期待件数の分母に残し、固定理由で区別する。

このコマンドは現在のTypeScript履歴replayを一時ディレクトリでbuildし、同じ入力へ実行する。
Frontend依存のインストール、Node、Backendのjsonschemaが必要。
一時入力は0600で作成して終了時に削除し、出力は0600の新規作成に限定する。
公開結果は専用schemaで固定キー・数値・固定enum・検証用hashだけを許し、
本文、session／response ID、時刻の個別列を含めない。測定commitと集計器・schema・replay実装のhashを別々に保持する。

音声の非ゼロ出力sample、復号済みpacket／sample、受信delta文字、応答中DOMの追加文字を別々に集計する。
各項目は観測／欠測、確実な残留、残留の可能性、確認済みゼロ、総量の上下限を持つ。
「残留の可能性」は「確実な残留」を含む。文字単位はUTF-16 code unit。
サーバーcancelをclient cancel受信時刻へ置き換えず、時計の不確かさも上限へ残す。

保存履歴再表示は開始通知のturn IDで相関し、独立チャネルへ集計する。
旧観測などに対応付けやDOM観測がない場合は`history_window_unobserved`として分母に残す。
v1.1はサーバー側のprovider結果受領を、文字数と音声payload byte数へ集計する。
provider内部の生成時刻や音声sample数への読み替えは行わない。
旧v1.0のサーバー生成の未実装項目と過去artifactはそのまま保持し、schemaは両versionを区別して検証する。
100件条件と全観測範囲の充足を別途検証する。
終了コードは合格0、集計保存済みだが受け入れ未達1、入力・schema・実行失敗2。
このstale集計だけで通常応答、VAD、再接続や#150全体の達成とは判断しない。

起動前の失敗などでtrace自体が作られなかった場合も、指定したtraceパスが未存在であることを確認し、
そのhashを`null`として失敗・欠測を集計する。空ファイルのhashを代入しない。
traceが存在するのに壊れている場合や読めない場合は入力失敗とする。


## 受信PCMからSTT入力までの連続範囲

音声測定を有効にしたrunでは、Bridgeが受信したPCM16 byte列の連番に対して、
各captureの開始／終了sampleと連続性を`stt_capture_received_*`へ記録する。
2秒のprerollを短縮した場合は、実際に保持した末尾範囲を起点にする。
追加PCMの位置が飛ぶ、重なる、逆行する、sample途中で分かれる、またはcapture長と一致しない場合は
`stt_capture_received_span_valid=0`とし、検証済みの開始／終了位置を出さない。

最終STTの直前には、入力前sample数、除いたprefix、入力後sample数と、
実際の入力byte列が元captureの全suffixと一致するかを`stt_input_*`へ記録する。
冒頭STTも試行番号付きの`stt_preview_attempt_N_input_*`へ同じ比較を記録する。
波形・本文・音声hashを診断へ追加しない。STTへ渡すbyte列、切り出し、待機時間は変更しない。

この連番はBackendが受信したPCM上の位置であり、ブラウザfixtureのsource sample位置や
送信RTP packet番号ではない。連続範囲とsuffix保持だけでOpus前後の発話境界や全STT処理を証明しない。
固定fixtureの正解境界との対応、サービスへ届いた入力、正式100試行の集約は別途照合が必要である。


## 取消後にCoreへ届いたprovider結果

`provider_result_*`は、Coreの実streaming処理がLLM／TTSのiteratorから結果を受け取った地点を観測する。
その時点で同じ応答がCANCELLEDなら、テキストの受領件数／UTF-16 code unit数と、
音声の受領件数／payload byte数へ加算する。後続の受付制限で破棄される結果も数える。
同じpayloadが複数回yieldされた場合も受領件数から除外しない。本文・波形は保持しない。

LLMとTTSのconsumerが両方終了してから、`provider_result_observation_closed=1`と最終数値を記録する。
consumerの取消要求だけでゼロ件の観測終了を出さない。payload型の異常はvalid=0にする。
これらは数値診断であり、正常cancelを処理失敗へ加算しない。

これはprovider内部の生成時刻ではない。音声byte数はWAV headerなどを含むprovider payloadで、
ブラウザの復号sample数・非ゼロ出力sample数とは別単位である。
ブラウザ側へ届かない結果も観測するためのサーバー受領境界として扱い、
未実装のprovider内部時計を推測で補完しない。


### ブラウザ最終出力段の停止確認

`PostGainAudioMonitor.stopAndConfirm()`は最終出力workletへstopを送り、それ以後の出力を
不可逆に無音化する。workletは照合済みrender frameで無音区間の終端を通知し、
monitorはその区間をブラウザの実出力時計が通過してからだけ確認を返す。
指示前の古いmarker、非ゼロ区間、不正なframe、時計欠測、閉鎖、timeoutを成功へ変換しない。
stopは監視のfinishとは別であり、取消境界以後の無音も独立に観測できる。
1000msのtimeoutは待機を打ち切る上限であり、#150のdecision後cancel 200ms目標を緩和しない。

`frontend/e2e/output-stop.spec.ts`は合成ページで実AudioContext・worklet・出力時計を使い、
入力sourceを動かしたまま最終出力を停止する。ページだけを置換し、音声処理・時計はモック化しない。
これはブラウザ最終出力段の検証であり、LiveKit経由の取消要求・停止確認・Core状態遷移を通す
受け入れ試験ではない。現在、この確認をCoreの取消成立条件へ接続する作業は未完了である。


## 人格を維持した合成履歴・記憶contextの比較

他の音声測定を終了させ、commit済みworktreeで実行する。`compare_context.py`は再接続cohortと同じ
排他lockを使うが、共有Ollamaを使う別プロジェクト全体を排他制御するものではない。
`--output`は`/tmp`以下の新規ディレクトリを指定し、既存結果は上書きしない。

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python scripts/voice_quality/compare_context.py \
  --inference-env /path/to/inference.env \
  --output /tmp/voice-context-comparison-01 --repetitions 3

PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python scripts/voice_quality/report_context.py \
  --run /tmp/voice-context-comparison-01 \
  --output docs/artifacts/voice-context-comparison-01.json
```

光織のCharacter CardとCharacter Bookの選択処理を維持し、固定した合成履歴0／2／8往復と
記憶0／1／4件の6条件を比較する。通常のCHAT optionsと、そのoptionsの`think`だけをfalseにした条件を使う。
各条件・設定で準備1回を除外し、指定回数を測定する。元の生成設定、実会話DB、記憶DBは変更しない。
履歴・記憶はprompt入力へ直接渡すため、検索・privacy判定・永続化の実接続テストとは区別する。

応答本文は保存しない。名前と固定した合成事実の出現だけをbooleanで保存し、未知の内容を補完していないかを
限定的に調べる。この一致を人格・記憶の品質全体の合格判定にはしない。匿名集計は全計画試行の存在、
重複、準備の除外、欠測と分母、時刻順序、schema、本文・IDの非混入を検証する。
要求の失敗や未観測値を除去・ゼロ補完しない。runnerのSHAが未記録の旧prototype結果はnullのまま残す。

測るのはLLM要求から最初の本文、prompt準備、provider報告のload／prompt evaluation／generationであり、
音声TTFAではない。`load_duration`を純粋なweight loadやqueue待ちと断定しない。
条件の順序や共有Ollamaの状態の影響があるため、単独の比較でthinkingだけの因果効果を証明しない。

## 観測区間を監査した通常応答のlatency比較

```bash
backend/.venv/bin/python scripts/voice_quality/compare_normal_latency.py \
  --candidate docs/artifacts/livekit-controlled-complete-playback-2026-09-08.json \
  --verification docs/artifacts/livekit-controlled-complete-playback-2026-09-08-verification.json \
  --output /tmp/voice-normal-latency-new.json
```

凍結WebSocket artifactのhash、candidateの検証記録とのhash一致、測定版にある監査済みsourceを確認する。
sourceが変更された場合、同名eventの観測区間を再確認するまでこの比較器は拒否する。
発話確定・応答決定・STT開始の3区間は実観測の意味が異なるため、相対比較不能と根拠を記録する。
絶対上限と他の比較可能指標は緩和しない。通常応答latencyだけの判定であり、
割り込み・reconnect・stale・dogfoodなどの独立cohortは別の受入証拠が必要になる。
既定`voice_metrics.evaluate_artifact`は変更せず、その結果とcoverage errorsも併記する。
欠測や未達でもレポートを保存してexit 1を返す。既存出力は上書きしない。

## STT入力PCMを照合する診断部品

`pcm_boundary_alignment.py`は16kHz PCM16の参照音声と実入力について、
正解発話の前半・後半から独立に選んだ高energy区間を正規化相関で照合する。
返すものはsample数、一致位置、相関値、推定損失上限だけで、PCMや本文は出力しない。
相関0.8未満、20ms以上離れた競合候補との相関差0.1未満、前後のoffset差2ms超、
短すぎる参照音声は未確認として残し、損失0msへ補完しない。

位置の許容幅2msは診断器の設定値であり、全音声の誤差保証ではない。
固定speech fixtureの実Opus encode/decodeでは前後が同じ104sampleのlookaheadを持つことを確認し、
欠落・重複・無音・前後で異なるoffsetを含む9件のテストが成功した。
これはcodecと照合部品の検証であり、実Browser→LiveKit→Whisperの入力観測や、
ラベル付き100試行のPCM境界受入はまだ証明しない。実入力の取得・試行との対応・集計を別途接続する必要がある。
診断用依存は`backend/requirements-dev.txt`に定義する。

## 実Whisper入力のPCM観測

```bash
backend/.venv/bin/python scripts/voice_quality/run_pilot.py \
  --run-id pcm-input-new --trials 3 --scheduled-fixture --disable-thinking \
  --inference-env /home/asa/dev/digital-souls/backend/.env --observe-stt-pcm
```

明示指定時だけ`integration-voice-pcm`を選び、runnerがlocalhost:50023の中継を所有する。
Whisperの実POSTを既存localhost:50022へバイト列を変えずに転送し、HTTP statusと応答本文を返す。
入力はメモリ内でfixtureへ照合し、数値だけを`whisper-input-pcm.jsonl`へ保存する。
fixtureはrepositoryの固定hashから選び、trial ordinalとinitial／labeledの別をNode側で登録する。
prepareの100ms無音は別記し、境界の成功試行へ数えない。

通常fixtureとラベル付きcohortに対応する。中継による追加処理を含むため、このProfileの
latencyを通常の無観測Profileの値と同じ条件として扱わない。既存の通常artifact集計器は
このProfileを受け付けない。PCM照合の成立や最終STTとの相関は別途検証する。
前半だけを使うturn判定用STTと最終STTを、request順だけで同じ意味とみなさない。
終了時は所有HTTP serverとrequest threadを閉じる。共有Whisper等は終了しない。

中継は、前後で同じoffsetを持つかという従来診断に加え、`edge_alignment`へ
正解発話の冒頭・末尾を必ず含む窓の直接照合を記録する。前後のoffset差を
許容値内へ丸めたり、従来の未確認結果を再解釈したりしない。
直接照合の成功は端部が入力中で識別できるという診断であり、途中の時間変化・無欠落を証明しない。

### 実PCMの匿名集計

```bash
backend/.venv/bin/python scripts/voice_quality/report_stt_pcm.py \
  --run-root frontend/test-results/livekit-quality/runs/pcm-input-new \
  --output /tmp/pcm-edge-report-new.json
```

manifest、実POST観測、Backend trace、解決済みProfileを照合する。試行番号とfixtureに加え、
Coreで確定した発話の最終STT sample数が一致する一意なHTTP入力を選ぶ。
previewのsample数や単なる最後のHTTP要求を代用にせず、曖昧なら欠測理由を残す。
正解の冒頭・末尾の位置、相関値、競合候補、範囲内のsample数と順序を再検証する。

100件の分母、全件の最終入力相関、端部照合、終了、測定revisionが揃う場合だけ
このPCM端部の検証範囲を合格とする。本文中の休止分割率、VAD判定時刻、
発話途中の無欠落、他の音声品質条件の合格は別途必要になる。
少数試行・欠測でもJSONを保存し、未達時はexit 1を返す。

### 2026-09-08 文中休止100件の実Whisper入力照合

`pcm-pause-100-20260908-01`（測定revision `6f1dc67303cef561a71a37f84aea868ea19e8401`）は100件すべてで会話動作と最終STT入力の相関を確認した。VADの先頭・早期終了・分割の検出基準は100件で通過したが、実PCM端部の厳密な照合は90件、未確認10件のためPCMレポートの`passed`はfalseである。検出時刻の合格を、実PCM端部の未確認の補完には使用しない。

結果は`docs/artifacts/livekit-stt-pcm-pause-100-2026-09-08.json`、検出結果は`livekit-vad-pcm-pause-100-2026-09-08.json`に保存した。測定が所有したFrontendとBackendの削除、観測proxyポートの閉鎖、および入力artifactのhashは`livekit-stt-pcm-pause-100-cleanup-2026-09-08.json`に記録した。未確認試行を除外せず、100件の分母を保持する。
