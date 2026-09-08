# 音声会話品質の計測

## 目的

移行前baselineとして凍結した現行WebSocket pipelineと、正式経路として実装するWave 3 LiveKit pipelineを同じ定義で比較する。比較はtransportの採否判断ではなくLiveKit版の受入判定にのみ使用する。計測は `automated_test`、`controlled_baseline`、`dogfood` を別run・別artifactとし、混在させない。schemaは [voice-quality-artifact-v1.schema.json](schemas/voice-quality-artifact-v1.schema.json) である。

## trace と clock

生traceは `event_id`、`character_id`、`session_id`、`utterance_id`、`response_id`、event名、stage、outcome、reason code、timestamp、clock domain、単位、数値観測値だけを allow-list で書き出す。`character_id`は人格単位の分離・検証・削除に使い、異なる人格のtraceを同じaggregateへ混在させない。prompt、transcript、音声本文、音声payload、exception詳細は書き出さない。server は `server_monotonic` / ns、client は `client_monotonic` / msを使い、異なる clock domain の値を直接減算しない。fixtureの発話開始・終了はfixture開始時とsample数からclient clock上の時刻へ変換する。

## 指標カタログ

| 指標 | 開始 | 終了 | clock / 単位 |
|---|---|---|---|
| utterance finalized | VAD speech end | utterance確定 | 同一clock / ms |
| response decision | VAD speech end | response開始decision | 同一clock / ms |
| STT開始 | user audio受信 | STT開始 | server monotonic / ms |
| STT処理 | STT開始 | STT完了 | server monotonic / ms |
| LLM開始 | STT完了 | LLM開始 | server monotonic / ms |
| first text | STT完了 | first text delta | server monotonic / ms |
| LLM完了 | LLM開始 | LLM完了 | server monotonic / ms |
| TTS開始 | first synthesizable segment | TTS開始 | server monotonic / ms |
| first audio | first synthesizable segment | first audio publish | server monotonic / ms |
| client playback | audio/track受信 | first playback | client monotonic / ms |
| TTFA | 利用者発話終了 | first AI playback | client monotonic / ms |
| local playback stop | speech start | local stop | client monotonic / ms |
| turn decision | speech start | backchannel/take_turn確定 | 同一clock / ms |
| cancel after decision | take_turn確定 | server cancel確定 | server monotonic / ms |
| barge-in cancel total | speech start | server cancel確定 | 同一clock / ms |
| VAD冒頭・終了境界 | fixture正解境界 | 実際の取込・確定 | fixture/client / ms・発生率 |
| stale出力 | cancel確定 | 旧responseの生成・受信・提示 | 件数 |
| reconnect | network疎通回復 | control/audioの双方利用可 | 同一clock / ms・成功率 |
| playback継続性 | 予定playout | 実frame供給・buffer | ms・件数・比率 |
| resource | run開始 | run終了 | CPU、memory、送受信量、network条件 |
| 処理失敗 | 応答対象utterance | stage outcome | 件数・失敗率 |
| 手動操作 | session開始 | 通常会話終了 | 回数 |

現行WebSocketで理由付き `not_applicable` とするのは、`local_playback_stop`、`turn_decision`、`cancel_after_decision`、`barge_in_cancel_total`、`stale_output`、`reconnect`、`playback_continuity` の7指標である。実装済み計測点のeventが来ない場合は `missing`、その指標に関係するstageが失敗を報告した場合は `failed` である。正常cancel、privacy skip、利用者終了、障害注入は関係する指標で `excluded` とし、失敗率の分子・分母に入れない。stage outcomeはSTT、LLM、TTS、transport、playbackをそれぞれのeventから選び、response全体の結果を一括投影しない。

## 集計と合否

p50とp95は Hyndman-Fan Type 7 で計算する。p50は診断値、latencyの合否はp95で判定する。rateはbasis point（`100 = 1%`）で、artifactに試行数、成功、失敗、missing、not applicable、除外、率の分母を残す。

LiveKitの絶対p95上限は TTFA 2000ms、local stop 3000ms、turn decision 3000ms、decision後cancel 200ms、全cancel 3500ms、utterance確定 800msである。local stopと全cancelは、VAD直後に相槌でも停止する旧方式ではなく、冒頭STTによる相槌／発話判定を含む。比較可能な同一区間では `WebSocket p95 + max(10%, 50ms)` も同時に満たす。TTFA p50 1000msは改善目安であり合否を変えない。CPU、memory、bandwidth、packet loss、network条件も診断値であり合否に使わない。

## controlled WebSocket baseline

Ubuntu-dev の `integration-voice` Profileで Backend、Ollama、Whisper、VOICEVOX、Chromium を起動し、日本語固定fixture `speech-v2`・同一初期状態でwarm-up 5回を行った後、独立session・独立conversationで100回を測定する。fixtureは日本語の単一発話とVAD確定用の後続無音を含み、正解transcriptは実際の発話内容と一致する。外部の Ollama と VOICEVOX を起動してから、repository rootで `npm run baseline:websocket` を実行する。runnerはwarm-up前にfixture version、WAVのSHA-256・sample rate、発話境界、期待transcriptを検証する。各試行では画面に確定した利用者transcriptをUnicode NFKC正規化、前後空白除去、連続空白圧縮して期待値と比較し、不一致なら停止する。通常の `test:integration:voice` とは別の `frontend/test-results/controlled-baseline/` を一時data rootとし、trial manifestにはfixture version、hash、transcript一致結果だけを残す。warm-upはaggregateに入れない。finalizerは回数・独立ID・fixture identity・全試行のtranscript一致・初期状態を再検証し、schema検証と再帰的な匿名性検査を通したaggregateだけを `docs/artifacts/websocket-baseline-v1.json` へ保存する。LiveKit版Wave 3 pipelineが計測可能になった時点でtransportだけを `livekit` とし、同じfixture、回数、schema、目標値で再実行する。

## LiveKit dogfood traceと集計

dogfood BackendはLiveKit sessionごとに、Conversation CoreのSTT／LLM／TTS stage、最初のtext／audio publish、Frontendから戻る`speech_stopped`／`playback_started`を`session_id`・`utterance_id`・`response_id`で相関して生traceへ記録する。STT失敗などprotocol上のresponseが作られない場合だけ、集計から失敗が消えないようtrace専用のresponse IDを発行する。計測ログにはprompt、transcript、text delta、音声、exception本文を保存しない。

`speech_stopped`と`first_playback`は同じ`client_monotonic` clockとしてTTFAを算出する。TTS開始はworkerのqueue待機開始ではなく、最初の合成可能segmentを受け取った時点とする。LiveKitで観測が未実装の指標はWebSocket用の`not_applicable`へ落とさず`missing`として残す。

受入対象のBackend起動で作られた同一`character_id`のtraceだけを選び、次のように匿名aggregateとMarkdown表を生成する。複数ファイルの場合は`--trace`の後へ列挙する。異なる`character_id`が混在した入力は拒否する。生成物には逆引き可能なcharacter／event／session／utterance／response IDを含めない。

```bash
PYTHONPATH=backend backend/.venv/bin/python -m app.livekit_trace_report \
  --trace "$DS_DATA_DIR"/voice-metrics/raw/<対象trace>.jsonl \
  --output docs/artifacts/livekit-dogfood-<実施日>.json \
  --schema docs/schemas/voice-quality-artifact-v1.schema.json \
  --run-id livekit-dogfood-<実施日>
```

browser内だけで確定するclient track受信、barge-in local停止、reconnect、playback continuityは、`test:integration:voice`のPlaywright添付証跡と#112の手動受入記録を併用する。Backend生traceに対応eventがない場合、aggregate上も`missing`のまま残し、測定済みに見せかけない。

### 手動で取得したdogfood resourceの取り込み

同じ受け入れrunで取得した数値を、`voice-dogfood-resource-observations-v1.schema.json`に従うJSONへ記録し、上記コマンドに`--resource-observations <観測JSON>`を追加する。この入力は手動測定の申告であり、実サービスの自動テストや測定済み証拠を生成する機能ではない。

入力は`schema_version: "1.0"`、`measurement_kind: "dogfood"`、`method: "manual_docker_stats_and_host_gpu_v1"`、選択した全traceファイルのSHA-256配列`trace_sha256`、`samples`配列を持つ。受け入れ終了後のtraceに対して`sha256sum`でhashを取得する。選択traceとの不一致、重複、別の測定種別、未知の項目は拒否する。観測JSONも生traceと同様にリポジトリ外で保管し、公開artifactへはhashやIDを転記しない。

各sampleの必須項目は次のとおり。取得できなかった数値は`null`とし、0で埋めない。

| 項目 | 観測方法・単位 |
|---|---|
| `elapsed_ms` | 最初のresource観測を起点とする経過ミリ秒。sampleごとに単調増加 |
| `cpu_percent` | 同じBackend containerの`docker stats --no-stream`のCPU%。1 core占有が100%なので100を超える値も許容 |
| `memory_bytes` | 同じ出力のメモリ使用量をbyteへ換算。Docker CLIがcacheを差し引いた値であり、container charged bytesやRSSとは区別 |
| `gpu_utilization_percent` | 同じhostのGPU device使用率の最大値。0〜100% |
| `gpu_memory_bytes` | 同じhostの全GPU device使用メモリ合計をbyteへ換算 |

CPUは取得sampleの単純平均、メモリとGPUは取得sampleの最大値を集計する。全期間の時間加重平均やsample間の瞬間peakとは扱わない。`resources.collection`へ観測方法、scope、全sample数、観測時間幅、最大間隔、指標ごとの観測・欠測件数を保存する。全sampleが未取得の指標は`manual_dogfood_sample_not_recorded`として欠測を残す。共有GPUには他processの負荷も含まれ、Backend専有値として解釈しない。

この取り込みはresourceだけを補う。通信量・packet loss、session中の追加操作、予期しない終了、再生品質は各々の観測が必要であり、resource値から推定しない。

### 製品traceのRTP統計

完全再生を確認した応答では、ブラウザが取得した送受信byte数・packet数・downlinkの累積lossを、`network_summary` observationとして同じsession/responseへ送る。IP、SSRC、stats ID、本文は含めない。Backendは既知の応答と生成PCMの総packet数を照合し、欠測理由を含むsnapshot全体を検証してから`network_rtp_*`の数値traceを記録する。重複snapshotや矛盾した値は混合しない。

dogfood集計はこのtraceからnetwork metadataを作る。複数発話を結合した応答もRTP snapshotは1回分として合計する。APIの欠落・timeout・未更新のpacket数は欠測理由と分母を保存し、計測失敗だけでは応答処理を失敗扱いにしない。負のlossは別streamの正のlossを相殺せず、件数として記録する。従来のtraceにnetwork観測がなければ欠測を残す。

この経路は完全再生した応答のRTP観測を対象とする。取り消し途中の応答やsession全期間のすべての通信を収集済みとは扱わない。新しい通常測定manifestは`network_measurement_method: native_observation_summary_v1`を宣言し、集計時に各応答の製品traceとブラウザ観測の一致を必須にする。ブラウザだけの値で製品側の欠測を埋めない。

## 保存と削除

dogfood生traceはリポジトリ外の `DS_DATA_DIR/voice-metrics/raw/` へ保存し、7日を超えたファイルを起動時に削除する。生traceはGit、会話履歴、テスト成果物、dogfood backupの対象にしない。長期保存aggregateからはcharacter・event・session・utterance・response IDを除く。リポジトリ内へ誤出力した `voice-metrics/raw/` は `.gitignore` で追跡対象外にする。

### 障害runnerとBrowserの時計対応

専用bridgeの制御probe診断では、Python障害runnerとBrowserへそれぞれnonce付き標準入出力要求／
`page.evaluate`を送り、Node側の要求送信・応答受信時刻でremote時刻を囲む。
起動時間は較正の往復時間へ含めない。Pythonのns値は十進文字列で渡す。
診断前後に各5sampleを保存し、両方を説明できるoffsetの共通区間から
`fault_runner_monotonic`→`client_monotonic`の上下限を求める。
Browser時計の量子化を考慮して各sampleの両端へ0.2msの余裕を付ける。
区間が交差しない場合や合成した幅が20msを超える場合は測定失敗とする。

復旧時刻の上限を過ぎて送信した要求のみを復旧確認の候補にし、遅延は復旧時刻の下限から
算出する。復旧前の要求に対する遅着ackや、誤差区間内の送信で成功率を補完しない。
この時計較正だけでは音声復旧・重複再生・再接続100試行の受け入れを証明しない。


## 取消前の出力停止確認と独立再集計

LiveKitの停止要求にはsession、response、transport generationと要求IDを付ける。
通常の出力経路では最終AudioWorkletを不可逆に無音化し、無音化位置を実出力時計が通過してから確認を返す。
サーバーは音声sourceの停止とその確認を待ち、生成stageの終了と合わせてCoreの取消を確定する。

明示的な診断では、ブラウザの各出力graphに別々のIDを付け、要求、停止位置、時計通過の一次記録を
既存の出力・時計archiveへ保存する。サーバーの`output_stop_confirmed` event IDは対応する要求IDと一致する。
独立replayは同じsession・response・generation・graph・要求ID、出力区間の連続性、停止位置の無音、
実出力時計の通過、停止後の非ゼロ出力の不在、archiveの閉鎖を検証する。サーバーの時刻は整数文字列で受け、
要求、source停止、出力停止確認、Core取消下限・上限の順序を整数精度を保ったまま照合する。

この照合に成功した場合のみ、取消前に実出力を通過したframeを取消frame下限へ加える。
時計probeから求めた元の取消時刻上下限は変更しない。欠落、競合する要求、別graph、非ゼロ出力、
逆転したサーバー時刻や未閉鎖の観測をゼロと扱わない。要求の一次記録がない旧測定は従来の時計照合で再集計する。

stale report v1.2の`output_stop_evidence`には検証済み、未検証、利用不能の試行数のみを保存し、
その合計を予定試行数と一致させる。生ID、本文、音声は公開aggregateへ出さない。
v1.0/v1.1の保存済みartifactと当時の欠測・不確かさは変更しない。
出力停止の確認だけで正式100試行や他の音声品質受入条件を満たしたとは判定しない。


## Provider内部時刻の取得可否

LiveKitの匿名集計にはHTTP要求からprovider受付、provider受付から生成開始、生成開始から本文の
最初のtoken受信まで、およびOllamaの独立したqueue待ちを別指標として残す。
現在利用するOllama `/api/chat` の応答には内部受付・生成開始のclockと独立したqueue待ちがないため、
adapterは`ollama_internal_timing_unavailable`を記録する。このmarker自身の記録時計を内部時刻には使わない。

markerがある試行は4指標を`missing`・`ollama_api_internal_timing_not_exposed`として分母に残す。
未計測の旧traceへ取得不能markerを後から追加せず、旧traceは必要eventの欠測として扱う。
HTTP header受信時刻とtotal/load/prompt evaluation/generationの残差を、受付・生成開始・queueの値へ変換しない。
異なるclockの境界や、取得不能markerと内部時刻の矛盾も欠測として残す。
これは内部queueを実測できるようになったという意味ではなく、取得不能の理由を明示する変更である。

### 応答全体の再生品質を通常traceへ記録する

完全再生を確認した`playback_completed`は、任意の`playback_summary`で出力sample数、RTP範囲、gap、出力時計を送る。途中prefixの通知には添付できない。Backendは現在待機している応答と末尾sequenceの完了gateが受理した通知だけを扱い、送信済みPCM量、padding、連続RTP、出力時計の通過を検証する。音切れがある場合も実測値を保持し、不整合をゼロへ置き換えない。

記録するのは`scheduled_playout`、`frame_playout`と、gap合計・最大gap・underrun回数・再生時間の4値である。前者2点は`browser_audio_context`の同一時計で比較する。controlledの集計では製品traceと測定manifestの値が一致することを検証し、重複、部分的な記録、矛盾を拒否する。旧runで製品側の6観測がすべて存在しない場合のみ、従来どおりmanifestの検証済み値を使用する。

これは応答全体を再生した場合の計測経路であり、途中キャンセルした音声全体の品質や、実声dogfoodの受け入れ完了を示すものではない。dogfoodの実測は所定の配備・手動受け入れ手順で別途実施する。

2026-09-08の実サービスpilot（`native-playback-summary-pilot-20260908-02`、revision `74b026e`）で、準備1件・測定3件すべてに6観測が製品traceへ届き、manifestとの一致、匿名aggregate、所有コンテナの終了を確認した。測定3件のgap・underrunは0だった。これは通常計測経路の動作確認であり、実声dogfoodの受け入れや100件の品質判定ではない。直前のpilotはfixtureの開始時計の幅が45.4msとなって失敗した結果を保持し、実quantumで無音処理が可能なことを確認してからfixtureを開始する修正後に再検証した。20msの上限とfixtureのPCMは維持した。


## 通常音声応答の低遅延設定

Conversation Coreから音声の本文をstreamする要求には`latency_sensitive=true`を付ける。Ollama adapterは、この要求で`think`が明示されていない場合に`think: false`を送る。CHAT optionsに明示した`think: true`または`false`は優先する。通常のテキスト要求は従来どおりproviderの既定値を使う。

この指定によって人格、履歴、RAG、現在の発話、context長、出力上限は削らない。adapterへ渡すoptionsは複製し、共有設定を変更しない。計測traceには要求数として`llm_latency_sensitive_requests`、`ollama_thinking_configured_requests`、`ollama_thinking_disabled_requests`を記録する。これらは同一計測内で合計される件数であり、設定の最終状態や本文の品質評価ではない。

実サービス検証では`run_pilot.py`の`--disable-thinking`を付けず、通常経路からこの指定が送られることを確認する。過去の同flag付き実験は当時の実験条件として保持し、通常設定の性能証拠へ読み替えない。低遅延設定の関連テスト105件が成功した時点では、新設定の実接続100件および実声での会話品質受け入れは未完了である。

## PCM端部照合の100件再検証（2026-09-08）

`pcm-witness-v3-pause-100-20260908-01`では会話動作100件と実際の最終STT入力100件を確認したが、端部照合は98件成功、2件未確認だった。先頭の位置を求めるseedの相関が閾値未満であり、未確認2件を正常へ補完しない。[PCM結果](artifacts/livekit-pcm-witness-v3-pause-100-2026-09-08.json)、[VAD結果](artifacts/livekit-vad-pcm-witness-v3-pause-100-2026-09-08.json)、[終了確認](artifacts/livekit-pcm-witness-v3-pause-100-cleanup-2026-09-08.json)を保存した。共有サービスを残し、測定が所有したFrontend・Backendと観測proxyの終了を確認した。この測定は端部品質の受け入れを満たしていない。

通常設定の最初の実接続pilot（`voice-default-latency-pilot-20260908-01`、revision `1c060e9`）は準備1件・測定3件すべてで低遅延要求と`think: false`の送信を確認し、会話・再生・所有環境の終了を確認した。一方TTFA p95は11334.77msで未達だった。Ollama load p95は9605.52msで、計測中の常駐contextに要求した8192と異なる13024が観測され、モデル不在の観測もあった。同時要求の発生元は未確認であるため、原因を断定しない。[匿名集計](artifacts/livekit-voice-default-latency-pilot-2026-09-08-01.json)と[設定・終了照合](artifacts/livekit-voice-default-latency-pilot-2026-09-08-01-verification.json)を保存した。traceの`unit`はtimestampの単位であり、これらの要求数はaggregateでは`count`として扱う。


## 端部位置の複数候補照合

端部照合v4は、発話先頭・末尾それぞれの最大600ms内で、高エネルギー位置と固定した始端・中央・終端の最大4候補を照合する。各候補は従来と同じ最大200msのseed、相関0.8以上、他位置との差0.1以上を要求する。採用する端部の75ms特徴窓、探索幅20ms、filterの片側4msを合わせた99msの範囲は維持する。正常な特徴窓を示した候補同士が20msを超えて食い違う場合は未確認にする。

全候補を数値証拠へ保存し、集計で固定位置の存在、選択候補、相関、局所窓、順序、候補間の一致を再検証する。中央部分の連続性や全周波数帯の音質は、この照合では証明しない。旧v3の保存結果はschema上で読めるが、v4の判定へ読み替えない。現在の再集計器はv4の候補証拠を要求する。

通常latency比較器は、再生完了の製品trace追加後の`production.py`を再監査した。変更はsource統計の通知順と最終playback summaryの受信であり、比較不能とした3区間の起点・終点は維持されている。旧版と新版の監査済みファイルhashを別々に保持し、artifactには測定revisionから実際に読んだhashを記録する。未知の変更は再監査を要求する。これは比較器の準備であり、新設定100件の性能合格を示さない。

v4の校正では301音声×9条件について、[未圧縮](artifacts/livekit-pcm-multi-seed-v4-raw-2026-09-08.json)、[Opus 32kbps](artifacts/livekit-pcm-multi-seed-v4-opus32-2026-09-08.json)、[Opus 64kbps](artifacts/livekit-pcm-multi-seed-v4-opus64-2026-09-08.json)のすべてで正常301件を照合し、欠け・無音化・雑音・重複・順序逆転の2408件を拒否した。実際のWebRTC入力による100件の受け入れは別途必要である。関連テスト48件と、旧schema互換性を追加した集計テスト22件（前者と重複を含む）が成功した。

v4実ブラウザpilot（`pcm-multi-seed-v4-pause-pilot-20260908-01`、revision `f942001`）では前回未確認だった5・66番と100番の音声を使い、3件とも実際の最終STT入力と端部を確認した。通常音声設定を用い、実験用thinking上書きは指定していない。[端部照合結果](artifacts/livekit-pcm-multi-seed-v4-pause-pilot-2026-09-08.json)と[所有環境・proxyの終了確認](artifacts/livekit-pcm-multi-seed-v4-pause-pilot-cleanup-2026-09-08.json)を保存した。最小端部相関は0.95749で、発話全体の均一offsetが成立しない1件もそのまま記録した。独立100件には達しておらず、全受け入れは未完了である。


## 通常音声E2Eと実通信障害の回帰検証

`npm run test:integration:voice`の割り込み検証は、再生中にラベル付き`take-01.wav`を入力し、実際のVAD・STT・意図判定と旧応答の取消を通す。停止・取消はfixtureの正解発話開始からも上限を評価する。人工的な`speechStarted()`の送信は使わない。

localhostのWebRTCを切断できない`context.setOffline()`による旧再接続テストは、既存の実通信障害suiteへ統合した。通常の音声E2Eと再接続は、両方を実行して回帰確認する。Frontendディレクトリから次を実行する。

```bash
npm run test:integration:voice
npm run test:integration:voice:reconnect -- \
  --run-id <未使用のrun名> --inference-env <開発用推論設定のパス>
```

再接続コマンドは`run_pilot.py`と`livekit-quality.spec.ts`の実障害分岐を使い、[専用障害環境の手順](../infra/voice-quality/README.md)で事前に起動したbridgeのlabel・単独接続・専用loopback portを検証する。共有SFUやdogfoodは切断対象にできない。controlと実音声の双方の回復、同一sessionでの次発話、新しい応答の完全再生、sessionと所有app環境の終了を確認する。`--trials 3`は同一sessionの障害前probe数であり、独立した再接続3試行でも正式100試行でもない。正式再接続100件は既存の`run_reconnect_cohort.py`で別途測定する。


## v4の実入力100件の結果

`pcm-multi-seed-v4-pause-100-20260908-01`（revision `ec79b4a`）では文中休止を持つ独立100件の会話動作、実際の最終STT入力、音声端部をすべて確認した。[PCM結果](artifacts/livekit-pcm-multi-seed-v4-pause-100-2026-09-08.json)の未確認は0件、最小端部相関は0.86580だった。同じmanifest hashに基づく[VAD結果](artifacts/livekit-vad-pcm-multi-seed-v4-pause-100-2026-09-08.json)は冒頭誤差・早すぎる終了・境界の不確かさ・文中休止の誤分割が各0件だった。発話全体の均一offsetが成立しない17件は残し、内部連続性の証明には使わない。

[終了確認](artifacts/livekit-pcm-multi-seed-v4-pause-100-cleanup-2026-09-08.json)で所有Frontend・Backendの削除、proxyの停止、通常音声設定での実行、両集計の入力一致を確認した。この結果は文中休止cohortのVADと端部の証拠であり、通常設定のTTFA100件、別cohort、実声dogfoodや#150全体の受け入れ完了を示すものではない。旧v3の98/100という未達結果も保存したままである。

実音声barge-inの回帰検証では、最初は非同期の最終意図判定を待たずに確認して失敗したため、判定到着を待つよう修正した。次の実行で停止988.5ms・取消1023.5ms（正解発話開始の下限から計算）を確認したが、終了時のSDK例外を4件記録したため全体成功にはしていない。[初回結果](artifacts/livekit-real-barge-in-regression-2026-09-08-01.json)と[終了時例外を含む結果](artifacts/livekit-real-barge-in-regression-2026-09-08-02.json)を保存した。実SDKでは`UnpublishTrackError`が`livekit.rtc`直下に再exportされていないことを確認し、実装と代替SDKを定義元の`livekit.rtc.participant`へ合わせた。取消を別の例外へ置き換えず音源を解放する回帰検証を含め、関連23テストと型検査が成功した。

SDK例外の修正後、通常音声E2E全5件は通ったが、終了・切断後の最終通知を送信しようとしてdelivery失敗2件を記録した。[この結果](artifacts/livekit-voice-regression-suite-2026-09-08-02.json)も未達として保持した。coordinatorが`unavailable`または`ended`の場合は、送信を取消としてCoreへ伝えるようにした。成功として記録したり、送信queueへ積んだりしない。初期化前の送信は引き続きエラーとし、再接続後は新しい送信を受け付ける。切断・終了・再接続、Coreのstreamingと停止確認を含む95テストと型検査が成功した。

`41bb2a2`で通常音声E2E全5件を再実行し、3往復の完全再生、実音声barge-in、観測対象のCore/pipeline例外0件、所有環境とログreaderの終了を確認した。[回帰結果](artifacts/livekit-voice-regression-suite-2026-09-08-03.json)は通常音声設定の実接続検証であり、正式100件の性能測定や実声dogfood受け入れとは分けて扱う。

再接続の回帰初回はcontrolと実音声が2343.09ms以内に回復し、重複出力0件だった。一方、テストは切断前に中断された初回応答も含めて2応答の完了を待ち、復旧後の新しい応答が完全再生されていても失敗した。[初回結果](artifacts/livekit-reconnect-regression-2026-09-08-01.json)を保存し、同一session・異なるresponse・新しいfixture開始以降の発話／再生に限定して、その応答の完了と完全再生を直接待つように修正した。復旧時間の10秒窓、3秒の目標、重複出力の条件は変更していない。

修正後の実障害回帰（`voice-regression-reconnect-20260908-02`、revision `3c0b21f`）では、controlと音声が2296.37ms以内に復旧し、重複0・欠測0、同一sessionでの次応答の完全再生を確認した。[独立再集計](artifacts/livekit-reconnect-regression-2026-09-08-02.json)はnative SDK・時計・packet出力・次応答を再検証している。単一sessionなので正式100試行の合否はfalseのままにした。[終了照合](artifacts/livekit-reconnect-regression-2026-09-08-02-verification.json)で所有app・専用SFU・bridge・時計runnerの終了を確認した。通常音声E2E5件と合わせた回帰検証の成功であり、#150全体の受け入れとは区別する。

通常設定の再pilot（`voice-default-latency-pilot-20260908-02`、revision `eb80708`）では、準備1件・独立測定3件が会話・完全再生・終了まで成功し、TTFA p50は1766.50ms、p95は1771.36msだった。Ollama load p95は257.90msで、観測した対象contextは8192のみだった。[匿名集計](artifacts/livekit-voice-default-latency-pilot-2026-09-08-02.json)と[設定・終了照合](artifacts/livekit-voice-default-latency-pilot-2026-09-08-02-verification.json)で、実験用上書きなしの通常経路と要求数counterを確認した。小規模測定ではTTFA目標内だが、準備5件＋独立100件による正式確認は別途必要である。前回の11.3秒という未達結果は保持している。

### 通常音声設定の独立100件（2026-09-08）

revision `95afacecf8e3cc55da85f0c53550479486bb8166`で、実験用thinking overrideを使わず、準備5件・独立session/conversationの通常応答100件を測定した。[匿名集計](artifacts/livekit-voice-default-controlled-100-2026-09-08.json)と[相関・終了検証](artifacts/livekit-voice-default-controlled-100-2026-09-08-verification.json)を保存した。全105件で通常音声のlatency policy、transcript・初期状態、6件の製品側再生観測とmanifest、全sample再生、session終了を照合した。所有appコンテナの削除とteardown完了も確認した。

本測定100件のTTFAはp50 1,775.900ms、p95 1,845.425ms、発話確定はp95 299.950msだった。絶対目標は両方達成し、TTFA中央値1,000msの改善目安は未達だった。処理失敗0件、追加操作0回。Ollama contextは8,192だけを観測した。

ただし、1件で再生gapが1回、1,984 samples（41.333ms）発生した。全sampleは再生されたが、制御測定のunderrun 0件は未達である。gap/underrunのp95は0でも、1件の発生を合格へ変換しない。[baseline比較](artifacts/livekit-voice-default-normal-latency-2026-09-08.json)では比較可能な8指標が許容範囲内、観測区間が異なる3指標は比較対象外、VAD冒頭・末尾の2指標はこのrunでは欠測として全体判定をfalseにした。実声dogfoodと#150全体の完了は示さない。

run中のmanifestにはmeasurement revisionがあったが、最終保存時にその項目が失われた。このrunのrevision根拠はrunner事前検査と、測定中に変更していないcheckoutの観測であり、検証JSONに根拠と最終manifestの不足を明記した。


### 通常PCM・供給診断pilot（2026-09-08）

revision `465abd6`の準備1件・測定3件で、通常音声の実Whisper入力端部と再生供給の数値診断を確認した。[PCM集計](artifacts/livekit-normal-pcm-supply-pilot-2026-09-08-01.json)は3件すべての最終STT入力・端部を相関したが、100件未満なので全体判定はfalseである。[照合・終了確認](artifacts/livekit-normal-pcm-supply-pilot-2026-09-08-01-verification.json)で全4件のpacket数・sample数・gap件数と完全再生記録の一致、診断欠測0、所有appとproxyの終了を確認した。測定3件のgapは0、受信間隔の最大上限32.600ms、復号待ち上限2.100ms、main配送待ち上限2.300msだった。この小規模runだけでは先行100件の音切れ原因や解消を証明しない。


### 通常PCM品質100件（2026-09-08）

revision `2f2a3857ea34cd10f77a849e5b15a981baf48d18`で準備5件・通常音声の独立100件を実行した。[PCM端部集計](artifacts/livekit-normal-pcm-supply-100-2026-09-08-01.json)では、最終Whisper入力の相関100/100、v4端部照合100/100、欠測0、session終了100/100だった。全体の時間伸縮が均一でない11件も診断値として保持し、内部PCM全体の品質を証明したとは扱わない。

[packet供給の照合と終了確認](artifacts/livekit-normal-pcm-supply-100-2026-09-08-01-verification.json)で、全105件のpacket数・再生sample数・gap件数が製品側の完全再生記録と一致し、診断欠測・上限超過は0だった。本測定100件のgap/underrunは0。最大の受信間隔上限46.900ms、復号待ち上限3.100ms、main配送待ち上限3.500msを記録した。所有appの削除・proxy停止・teardown完了、実験用thinking overrideなしも確認した。

これはPCM observerを介した入力品質・供給診断条件であり、通常latencyの測定と分離する。先行する通常設定100件の41.333msの音切れは再現しておらず、その原因特定や修正済みを示す結果ではない。
