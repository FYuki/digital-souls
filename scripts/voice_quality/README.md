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
