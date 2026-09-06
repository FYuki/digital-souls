# LiveKit音声品質の少数試行診断（2026-09-06）

## 適用範囲

#150の原因切り分け用のpilotであり、正式受入の結果ではない。各runはwarm-up 1回を除外した独立session・conversation 3回。同一の日本語音声fixture `speech-v2` を使用し、画面に確定したtranscriptの一致と応答完了を確認した。専用worktreeと`integration-voice`の一時data rootを使用する。既存dogfoodへはdeployしていない。

モデルはOllama `gemma4:e4b`、Whisperは`medium`・CUDA・int8_float16、TTSは既存VOICEVOX。下表の両runでは、実験用にCHATのOllama `think` をfalseとした。通常設定の既定値は変更していない。使用した`integration-voice`は既存のprofile定義に従いRAG無効である。人格promptの削減は行っていないが、記憶ありdogfood会話の性能を示す比較ではない。

## 実接続の比較

| 指標 | token計測cache導入前 p50 / p95 | 導入後 p50 / p95 |
|---|---:|---:|
| TTFA | 2921.8 / 3120.7 ms | 1979.7 / 1994.5 ms |
| prompt準備 | 1046.7 / 1064.5 ms | 15.7 / 20.5 ms |
| STT完了から最初の本文 | 1382.2 / 1399.2 ms | 400.4 / 403.3 ms |
| Ollama報告の生成入力token数 | 946 / 946 | 946 / 946 |

導入後の計測3試行はいずれもtoken計測cache hitが3回だった。未計測のhit数を0へ置換していない。導入後warm-upのTTFAは約15.3秒であり、上表から除外している。cold startの改善を示すものではない。

集計値の一次artifactは[導入前](artifacts/livekit-pilot-2026-09-06-think-off-before-cache.json)と[導入後](artifacts/livekit-pilot-2026-09-06-think-off-cache.json)。いずれもschema検証・匿名性検査を通している。3試行のp95が目標内でも、通常100試行の合格を意味しない。

cacheはadapter内の最大128件のLRUで、prompt本文を永続化しない。完全な計測要求をプロセス内のランダム鍵でHMAC化し、Ollamaのモデルmanifest digestを各lookup時に確認する。digest取得失敗時は通常のprovider計測へ戻す。cacheへ新規保存するときは計測前後のdigest一致を確認する。モデル変更・計測要求変更で誤ったtoken数を使わないための回帰テストを持つ。

## 計測境界と限界

- TTFAはfixture音声の正解終端から、AudioWorkletの最初の非ゼロ応答サンプルを`AudioContext.getOutputTimestamp()`でbrowser時刻へ写した時点まで。物理スピーカーの出音時刻ではない。
- fixture開始はfake microphone取得時のbrowser観測を使うため、音声デバイス内部の開始精度は未検証。正式測定へ向けて境界の精度を確認する必要がある。
- LiveKit受信・decode時刻、WebRTCのbyte数、process tree全体のCPU・memoryは欠測として保存する。playback時刻を受信・decode時刻へ流用しない。
- clientとserverのclockが異なるVAD境界指標は`metric_boundary_clock_mismatch`として欠測にする。server側の`utterance_finalized`所要時間だけで正解音声終端からの遅延が合格したとは判断しない。
- この日のartifactの初期状態hashは要求した「新規会話・空履歴」を表す文字列のhashであり、実際の記憶snapshotの同一性を証明しない。後続runnerでは各試行前に実DBを読み取り、空履歴・空記憶と人格card・memory policyのhashを記録する。この追加検証は過去artifactへ遡及しない。
- playbackの応答・segmentへの帰属は、受信metadataと出力frame数に基づく。jitter・再接続下でのstale提示や連続性の正解検証は未完了。

## 再実行

既存のWhisper・Ollama・VOICEVOXと、この作業専用のLiveKitを使用する。dogfoodのappやdata rootは指定しない。実験用CHAT optionsに`think: false`を設定した環境で、frontendから次を実行する。

```bash
VOICE_QUALITY_PILOT_TRIALS=3 node node_modules/@playwright/test/cli.js test --config playwright.livekit-quality.config.ts
```

LiveKit URL/key/secretと実接続サービス設定は環境から渡す。秘密値をartifactやcommand logへ転記しない。raw traceは複数runが追記されるため、必ずmanifestのsession/utterance/response組で抽出する。runごとのmanifest・trace・resolved profileは次の実行前に保存する。

```bash
PYTHONPATH=backend backend/.venv/bin/python -m app.livekit_pilot_report \
  --manifest frontend/test-results/livekit-quality/trial-manifest.json \
  --trace frontend/test-results/livekit-quality/runtime-data/voice-metrics/controlled-trace.jsonl \
  --profile-report frontend/test-results/livekit-quality/runtime-data/runtime/standalone/resolved-profile.json \
  --schema docs/schemas/voice-quality-artifact-v1.schema.json \
  --output /tmp/livekit-pilot.json \
  --run-id livekit-pilot
```

正式受入には、初期状態と計測境界の検証、warm-up 5回と通常100試行、相槌・take-turn各100試行以上、network障害・再接続、stale提示・連続性の評価が残る。内部思考無効化の応答品質と実声での自然さも別途評価する。

## 2026-09-07の初期状態検証

後続runnerはマイクを有効にする前に`app.voice_quality_state`を実行する。resolved profileとtest data rootの一致、対象会話の存在・未archive、会話turn・画面参照履歴・persona memory全tableが空であることを確認する。DBはread-onlyで開き、残存データを見つけた場合は試行を失敗させる。データの自動削除や空状態への置換はしない。

hashには実際の空table件数、schema version、人格cardとmemory policyの内容hashを含める。会話ID・作成時刻・UI設定はprompt初期状態から除く。実接続のwarm-up 1回と測定1回で同一hashを確認した。この検証は空状態の固定fixtureに限定され、記憶あり条件やモデル設定全体の同一性を代用しない。

## 2026-09-07の100試行run中断

warm-up 5回の後、測定7回は完了したが、測定8回目がFrontendの最初の再生観測待ち60秒で失敗し、runnerが停止した。100試行は未完了であり、受入artifactは生成しない。失敗runのmanifest・raw trace・Playwright結果は一時結果ディレクトリの`controlled-100-first-failure/`へ保存した。

失敗時点の専用DBには会話が1件残り、turnは0件だった。最後の成功試行より後のraw traceは0件で、途中相関を失敗manifestへ保存していなかったため、原因はこのrunだけでは確定できない。runnerへ本文を含まないCore event種別・相関ID・transcript文字数・マイク状態の失敗時保存を追加した。直前成功試行の`first_playback`を失敗試行の証拠へ流用しない。

## 停止原因の特定と試行cleanupの修正

追加診断で`OSError errno=24`（open file上限超過）を確認した。専用Backendのsoft limitは1,024で、停止前にはsocketが数百まで増加していた。内訳は主にWebRTCのTCP listen・UDP socketであり、SQLiteやtraceファイルを開きっぱなしにする漏れではなかった。

runnerは会話履歴を削除してpageを閉じるだけだったため、切断後60秒の再接続猶予中のLiveKit sessionが重なった。通常応答の試行間cleanupを、UIからsessionを明示終了し、終了APIの成功と`phase=ended`を確認してから履歴削除・page終了へ進む順序へ変更した。これは会話終了後の試行cleanupであり、3往復中の追加手動操作数とは別に扱う。socket上限や再接続猶予時間は変更していない。

controlled finalizerも全試行の`session_end_confirmed=true`を必須とした。過去の中断runを、この条件で成功した結果へ読み替えない。

## 明示終了後の20試行

warm-up 1回と測定20回が全件成功し、各sessionの明示終了を確認した。専用Backendのdescriptor数は活動中の約110から終了後35へ戻り、13回目の停止は再現しなかった。一次artifactは[明示終了後の20試行](artifacts/livekit-pilot-2026-09-07-session-cleanup.json)。

TTFAはp50 1,967.0ms / p95 2,065.9msで、p95 2,000msの目標には未達。STTは212.6 / 259.8ms、prompt準備は5.0 / 16.7ms、LLM最初の本文までの時間は377.9 / 392.1msだった。20試行の成功は通常100試行や割り込み・再接続の品質合格を示さない。

## 明示終了後の独立100試行

warm-up 5回と測定100回が全件成功し、transcript一致・応答完了・session終了・各試行前の空状態を確認した。Playwright全体は約11.2分。一次artifactは[独立100試行](artifacts/livekit-controlled-2026-09-07-session-cleanup.json)、比較結果は[latency評価](artifacts/livekit-controlled-2026-09-07-session-cleanup-latency-evaluation.json)。どちらも逆引き可能なIDや会話本文を含まない。aggregateはschema検証を通している。

| 指標 | p50 / p95 | 判定上の注意 |
|---|---:|---|
| TTFA | 1,972.1 / 2,053.1 ms | p95 2,000msに未達 |
| STT処理 | 214.0 / 258.1 ms | 実Whisper処理 |
| 最初の本文 | 379.2 / 395.0 ms | STT完了起点 |
| prompt準備 | 4.7 / 16.0 ms | 人格入力946tokenを維持 |
| 最初のaudio生成 | 369.6 / 434.2 ms | 合成可能segment起点 |

latency evaluatorは`passed=false`だった。TTFA未達に加え、割り込み、独立したclient受信・decode、VAD正解境界の欠測が残る。通常応答100成功をEpic受入合格へ読み替えない。

比較結果中の`stt_start_latency`、`utterance_finalized`、`response_decision`は、transport間の観測内容の差も精査が必要。WebSocketの`user_audio_received`は確定済みPCM payloadの受信、LiveKitはstreamの最初のPCM受信を記録する。またWebSocketの確定・decisionはBrowser側の送信準備、LiveKitの同名観測はSTT確定・Core応答開始である。同じevent名だけで、これらの相対値を同一区間の回帰と断定しない。凍結baselineや目標値は変更していない。

100試行後に、複数発話が末尾待機中の場合、先行発話のtimer完了が後続発話まで確定させる問題を回帰テストで再現した。各発話のtimer完了を個別に待つよう修正し、空音声の旧captureが後続を妨げないことも含めた関連70テストが成功した。この末尾待機修正は上記100試行より後の変更であり、上記artifactがその実接続検証を示すものではない。

## 今回の回帰検証

末尾待機修正後のBackend unit suiteは2,255 passed / 1 skipped、Frontend unit suiteは325 passed。Python lint、225 source filesの型検査、Svelte／TypeScript検査も成功した。画面参照の既存日付テストは、jsonschemaのformat検証が先に不正日付を拒否する環境で文言だけ不一致になったため、正常日付の受理と不正日付の拒否を保ったまま両方の拒否文言を許容した。

これらはunit／静的検査の結果である。Backend実接続suite全体、全品質cohort、末尾待機修正後の通常応答の実接続再測定、記憶ありdogfood・実声の自然さは未完了。PRはまだ作成していない。

## 受信・decode境界の追加検証

最初のremote trackについて、購読成立、RTCRtpScriptTransformでのencoded frame通過、MediaStreamTrackProcessorでの非ゼロdecoded sample取得を別々に観測する実装を追加した。encoded frame本体は変更せず、そのままdecoderへ渡す。decode観測はtrackのcloneを使用し、終了時に元trackを停止しない。APIが利用できない場合は理由を記録し、再生時刻で補完しない。

この実装はtrackの最初のmediaに限る。後続応答や再接続へ同じ時刻を流用しないため、現時点では初回応答のみ相関させる。複数応答・stale提示・再接続の受け入れ条件は未完了。実際の2つのRTCPeerConnectionと連続toneによる5試行では、encoded・decoded・AudioWorklet出力時刻の順序と元trackの継続を確認した。これはBrowserの計測APIの診断であり、LiveKitとConversation Coreを含む品質合格を示さない。

実接続pilotの初回再実行は、追加schemaの条件がAJVのstrictRequired検証に違反し、Frontend起動前に失敗した。schema条件を修正し、Frontend契約・Room・media observerの39テスト、Backend artifact finalizerの20テスト、Svelte／TypeScript検査が成功した。Roomテストの仮session IDも、実際の契約と同じUUIDへ修正した。この検証だけでは実接続の再測定済みとは扱わない。

修正後の実接続pilotは画面起動と音声応答まで進んだが、warm-upで受信・decodeがユーザー発話の確定より先に記録され、順序検証で失敗した。Browser相対時刻はencoded 2,090.8ms、decoded 2,093.8ms、発話確定3,660.6ms、最初の再生35,625.6ms。trackの最初のmediaが応答前に届いており、初回応答であってもtrack単位の先頭frameを応答の先頭frameと同一視できない。comfort noise等の可能性はあるが、今回の証跡だけでは音声内容は確定しない。測定3回は未実施で、成功artifactは生成しない。応答への帰属方法の修正が必要であり、単に順序検証を緩めて合格させない。

## track観測の誤相関を撤回

無音から440Hz toneへ切り替える実WebRTC診断で、無音中のencoded frameは`audioLevel=0`でも、decoded sampleには最大約0.000061の非ゼロ値が出ることを確認した。最初の非ゼロdecoded sampleを応答の開始と見なすことはできない。現行のRoom実装はtrack観測を保持し、`response_frame_correlation_unavailable`を付ける。応答IDへの推測による相関と、これに基づくclient受信／decode event送信を撤回した。誤相関が再び送られない回帰テストを含め、Frontend unit 330件とSvelte／TypeScript検査が成功した。

[WebRTC Encoded Transform仕様](https://www.w3.org/TR/webrtc-encoded-transform/#rtcencodedaudioframemetadata)のaudioLevelは送信元音量で、receiveTimeは最後の構成packetの受信時刻である。実Browserでこれらの存在を確認した。ただし、decoded frameと応答sourceの正確な対応は別途必要であり、音量閾値だけを追加して受け入れ合格とはしない。[LiveKitのframe metadata](https://docs.livekit.io/transport/media/frame-metadata/)は現行ではvideoのみで、AudioFrameのPython userdataをwire上の応答IDとして使うこともできない。

## ラベル付き300件のVAD・実Whisper診断

[fixture v1](../frontend/playwright/fixtures/voice-quality-v1/README.md)は相槌10語句、発話権を求める10語句、語句間に既知の無音を挿入する条件を各100件に展開する。ラベルはCore判定と独立に固定し、音声・境界・recipeの改変を拒否する9件のテストが成功した。語句反復による無音診断であり、自然な言い淀みの網羅やBrowserの割り込み100件を完了したものではない。

| 実Silero診断 | 相槌100件の未検出 | take-turn 100件の未検出 | 無音100件の誤分割 |
|---|---:|---:|---:|
| legacy・現行設定 | 17 | 2 | 6 |
| v5・同じ閾値と待機設定 | 60 | 16 | 13 |

legacyでは先頭欠落100ms超を含む件数がtake-turn 7件、無音条件6件で、いずれも目標未達。v5はこの条件で未検出が増えたため、アプリのモデルは変更していない。モデル変更だけで改善したとは判断しない。

legacyのVAD区間を実WhisperとCore判定へ渡した200件の診断では、相槌のうち83件に判定があり、17件はVAD未確定で欠測。判定ありの中に発話権取得の誤判定が30件あった。内訳は「そうなんですね」「そうですね」の各10件と、「へえ」が「ヘッ」と認識された10件。丁寧な相槌2種を相槌へ追加し、質問が続く場合はtake-turnを維持するよう回帰検証した。

修正後に同一200件を実Whisperで再診断し、相槌の誤判定は10件、VAD欠測は17件。take-turnは判定あり98件の誤判定0件、VAD欠測2件だった。これらは分類段階の診断値であり、Browserの誤cancel率・割り込み見逃し率の合格値として使わない。「ヘッ」をfixtureに合わせて相槌へ追加することはしていない。全stackの割り込み・再接続・継続性および品質目標の達成は引き続き未完了。

無音中の非ゼロdecodeを確認したため、初回応答を含めて、従来のmetadata到着後の非ゼロrenderとsource応答サンプルの対応も再検証が必要。保存済み100試行artifactのTTFAを確定した応答開始遅延として合否に利用しない。既存artifactは当時の算出結果をそのまま保存し、この制約を併記する。transcript一致・応答完了・明示終了100件の証跡と、sourceへの帰属が未検証な遅延値を分けて扱う。

今回の追加修正後、Backend unitは2,272 passed / 1 skipped、Frontend unitは330 passed。Python lintとBackend／Frontendの型検査は成功した。保存済みLiveKit artifact 4件のschema検証と、比較結果を含む5件の匿名性検査も成功した。schema検証・匿名性検査は、計測内容の妥当性や品質合格を示すものではない。

モジュール横断検証はBackend 1,421 passed、Frontend 97 passed。production buildも成功した。モックE2E全体とBackend／Frontendの実接続suite全体はまだ実行しておらず、各品質cohortの全stack受け入れも未完了。現時点の変更はDraft PRとしてレビュー対象にし、#150の完了やmerge可能状態として扱わない。


## 文中の継続・言い淀みfixtureとPCM終了判定（2026-09-07）

[fixture v2](../frontend/playwright/fixtures/voice-quality-v2/README.md)を追加した。相槌・take-turn各10語句に加え、別々の節を接続する10組（文の継続5組、言い淀み5組）を使用する。各cohort100件、計300件の開始位相・音量・既知の無音を固定する。自然な実声の評価とは区別する。

legacyの音声確率で発話を確認し、PCMの無音が600msを超えた時点で終了する検出器をLiveKitのcontinuous入力へ組み込んだ。従来のモデル確率の余韻が無音終了を遅らせることを避ける。背景音がPCM閾値を超え続ける場合は、確定済み発話を低い音声確率の継続でも終了する。未確定候補にこの終了条件を適用した初版では語頭欠落が増えたため、適用対象を確定済み発話に限定した。また、音声確率の強いframeの合算を直近2秒に制限し、離れたノイズのピークを無期限に合算しない。

サーバーのマイクprerollは800msから2秒へ拡張した。固定fixtureで発話確認まで最大約1,440msかかる例があり、元の語頭時刻を保持しても800msの音声bufferでは先頭が残らないためである。入力を停止したときは検出状態を破棄する。

保存済み実Silero確率と元PCMを使った修正後の診断は、相槌83/100、take-turn98/100、文中無音100/100で発話を検出した。検出済みの発話では冒頭100ms超の欠落・早期終了・誤分割は0件。未検出を成功分母から取り除かず、相槌17件とtake-turn2件を未達として残す。fixture末尾から検出終了までのp95は各cohort約757〜760msだが、これはオフラインのframe clockによる値で、Browser実時間や発話確定の受け入れ値ではない。

### 実接続での区間の分解

`pcm-vad-01`と背景音対策後の`pcm-vad-guard-01`は、それぞれ新しいdata rootで準備1回＋測定3回を実行した。既存の失敗試行の履歴・traceは消していない。両runともtranscript一致、応答完了、明示的なsession終了を確認した。測定scopeはpilotであり、独立100試行の完了ではない。

800msの受け入れ指標は[計測定義](voice-quality-measurement.md)の「VAD speech endからutterance確定まで」。fixtureの末尾からクライアントの確定event受信までの時間とは起点・終点が異なる。初回pilotの約1.1秒を800ms目標に比較した説明を訂正する。以下は測定3回のみで、server区間は同じmonotonic clockで差を計算した。

| run・試行 | VAD終了→STT開始 | STT処理 | VAD終了→サーバー発話確定 | LLM開始→最初の本文 |
|---|---:|---:|---:|---:|
| pcm-vad-01 / 1 | 150.4ms | 247.7ms | 398.1ms | 332.0ms |
| pcm-vad-01 / 2 | 150.0ms | 232.7ms | 382.7ms | 366.3ms |
| pcm-vad-01 / 3 | 150.3ms | 223.1ms | 373.4ms | 340.7ms |
| pcm-vad-guard-01 / 1 | 150.4ms | 204.1ms | 354.5ms | 4154.8ms |
| pcm-vad-guard-01 / 2 | 150.9ms | 267.0ms | 418.0ms | 6024.8ms |
| pcm-vad-guard-01 / 3 | 150.9ms | 218.8ms | 369.8ms | 16008.1ms |

fixtureの起点は現状`getUserMedia`完了時刻であり、実際の入力サンプル開始との対応を厳密には検証していない。この起点から求めたVAD境界誤差を正式な受入値にしない。応答sourceへのreceive／decode／playback相関も未解決で、両pilotは欠測理由`response_frame_correlation_unavailable`を残す。controlledモードはこの相関がなければ失敗し、pilot成功を通常100試行の証拠へ流用しない。

背景音対策後にもLLM最初の本文が約16秒かかった試行があり、TTFA目標の達成は示していない。記憶ありdogfood、各100件の全stack割り込み、実ネットワーク障害からの復旧・継続性、正確な応答サンプルの帰属、最終100試行の実測は引き続き未完了。


背景音対策後の単体検証はFrontend339件、音声runtime／fixtureの関連Backend47件が成功した。Svelte／TypeScript検査はerror・warningとも0件。修正後に非発声120件を実Sileroへ通した診断では候補検出器の誤起動9件、従来FrameProcessorの誤起動14件だった。誤起動が解消したという結果ではなく、自然環境の発生率へも一般化しない。

[連続背景音の診断](../frontend/scripts/measure-utterance-continuous-background.mjs)では、既存speech-v2へwhite noise／60Hz hum／pink noiseを各2音量で重ね、音声後もノイズを継続した。実Sileroによる6条件すべてで発話は1回終了し、終了遅延はfixture末尾から884〜1,172msだった。PCM閾値以上の環境音で終了不能になる回帰を防ぐ証拠であり、速い終了やBrowserの割り込み受け入れを示すものではない。


## 共有Ollamaのロード状態とGPUの同時計測（2026-09-07）

`pcm-vad-guard-01`でLLM最初の本文まで約4.2／6.0／16.0秒を要した区間を分解した。provider報告の`load_duration`は各3,744.7／5,623.0／15,801.9msで、promptのtoken計測は全件cache hit、計測合計は約3〜4msだった。観測後の共有Ollamaには13,312 tokenのcontextがあり、今回の要求の8,192 tokenとは異なっていた。ただし、この読み取りだけから、別タスクが遅延を引き起こしたとは断定しない。

[実測runner](../scripts/voice_quality/README.md)を追加して、別の新規data root `residency-01`で準備1回＋測定3回を実行した。全件のtranscript一致、応答完了、明示終了が成功した。今回の実測では遅い状態が再現せず、測定3回のLLM開始→最初の本文は次の値だった。

| 試行 | LLM開始→最初の本文 | provider load | prompt evaluation | token generation |
|---|---:|---:|---:|---:|
| 1 | 336.3ms | 273.6ms | 22.5ms | 122.6ms |
| 2 | 353.1ms | 267.4ms | 41.6ms | 88.5ms |
| 3 | 356.8ms | 269.0ms | 54.1ms | 193.3ms |

モデル状態127回、GPU13回を読み取り、観測APIの失敗は0件だった。開始直後の未ロード状態から8,192 tokenのロード済み状態へ遷移し、それ以降の取得サンプルには別contextへの変更がなかった。約0.5秒間隔の離散観測であり、間の変更やOllama内部queue待ちを排除できる証拠ではない。`load_duration`はv0.32.5の[ChatHandler実装](https://github.com/ollama/ollama/blob/v0.32.5/server/routes.go)上、handler開始からschedulerのrunner返却までを含むため、純粋な重み再ロード時間とは扱わない。

GPUはhost全体の使用率・使用／総memoryを取得した。この会話に帰属するGPU時間、Backend RSS、transport帯域・packet lossとは区別する。生観測はignoredのdata rootへ保存し、model名・digest・endpoint・本文・秘密値を含めない。`residency-01`の初版観測にはclock名`server_monotonic`が付いているが、実体はrunner自身のclockであり、Backendコンテナとの同期を証明するものではない。以後のrunnerでは`observer_monotonic`へ名称を訂正した。既存生観測を上書きしていない。

runnerの秘密値除外、欠測の維持、GPU単位・値域、run IDのディレクトリ逸脱防止、既存options維持を確認する19テストとPython lintが成功した。今回追加したのは診断情報と再現手段であり、TTFAの改善策や全必須指標の合格を証明したわけではない。応答sourceのreceive／decode／playback相関など、先に記載した未完了条件は維持する。


## 音声源のsample境界によるVAD測定（2026-09-07）

[固定PCMの音声源](../frontend/playwright/controlled-audio-fixture.ts)を追加した。マイク準備後に固定PCMをAudioWorkletからMediaStreamへ流し、source開始と正解発話開始・末尾のsampleを出すprocess呼び出しをmain→worklet→mainの因果関係で囲む。各時刻を20ms以下の幅の上下限で残し、VADや`getUserMedia`から正解時刻を推測しない。WAVのhashとPCM16 sample列は検証し、開始前の無音でfixtureを消費しない。

`source-clock-01`は準備1回＋測定3回が成功し、測定3回の発話末尾の観測幅は約0.7〜1.7msだった。VAD終了までの遅延は約754〜766ms。通常発話の語頭時刻が既存traceに出ないことも確認した。既存の`speech_started_client`は割り込みの旧応答専用に相関されるため、通常発話用の`vad_speech_start_client`を追加した。新しい観測はその発話への応答に結び、割り込み指標の旧応答相関は維持する。

追加後の`source-clock-02`も準備1回＋測定3回が成功し、transcript一致・応答完了・明示終了を確認した。以下の開始・終了誤差は正解境界の上下限を維持した範囲である。開始の負値はVADが保持するframe先頭が正解境界より前にあることを示す。VAD終了からサーバー発話確定までの800ms指標とは区間を分ける。

| 測定試行 | VAD開始 − 正解開始 | VAD終了 − 正解末尾 | VAD終了→サーバー確定 |
|---|---:|---:|---:|
| 1 | -61.2〜-60.4ms | 762.3〜762.6ms | 393.4ms |
| 2 | -65.4〜-61.6ms | 759.3〜762.4ms | 407.3ms |
| 3 | -60.6〜-57.9ms | 763.9〜766.1ms | 422.1ms |

この3試行では開始遅延100ms超は観測されなかったが、相槌・take-turn各100試行の成功率を示す結果ではない。通常のfake microphoneと入力供給方法が異なるため、凍結済みWebSocket baselineの計測方法と同一とは扱わない。入力側の正解clockを改善したもので、応答sourceへのreceive／decode／playback相関は未解決である。

Frontend単体351件、関連Backend66件が成功した。Svelte／TypeScript、Python lint、225 source filesの型検査も成功した。観測幅超過・欠測・時刻逆転の拒否、元PCM保持、開始前消費の防止、通知の重複防止を含む。発話末尾後のping停止は実接続2 runの後に追加した資源解放であり、上記runの観測値を変更していない。

### RTP受信とトラック配送の対応を調べる診断

[WebRTCの仕様](https://www.w3.org/TR/webrtc/#dom-rtcrtpreceiver-getsynchronizationsources)にある`getSynchronizationSources()`を、実際の2つのRTCPeerConnectionを結ぶ固定tone診断へ追加した。RTP timestampをキーにencoded受信とトラック配送を照合し、103/103 packetを対応付けた。トラック配送時刻はepoch基準なので`performance.timeOrigin`を引いてwindowのclockへ合わせた。受信→配送は約3.1〜23.1msで、時刻の逆転はなかった。既存診断を上書きしないよう、出力先指定と排他的な新規作成に対応した。

このAPIの時刻はMediaStreamTrackへの配送であり、スピーカー出力やアプリのAudioWorkletへの実sample提示の時刻ではない。最初の非無音packetに対応するdecoded frameでも、最初の10msは小さいcomfort noise、次の10msでtoneが観測された。同じRTP packetに相関できたことだけでfirst audible sampleを確定しない。アプリのresponseとの対応、stale提示、再接続後の世代境界は別途解決が必要である。

### 実LiveKitでの同一packet配送観測とレポート検証（2026-09-07）

`RemoteMediaObserver`へ、encoded frameの受信時刻と`getSynchronizationSources()`のトラック配送時刻を、SSRCとRTP timestampの両方で照合する処理を追加した。window／workerのclock原点を正規化し、時刻逆転・未来時刻・欠測を成功値へ置換しない。照合bufferは128件、観測窓は2秒とし、最初のpacketが遅れて届いた場合はその到着から窓を開き直す。終了・失敗時にはtimerを解放する。既存の非ゼロdecoded frame観測はcomfort noiseも含むため、`firstNonzeroDecodedFrameAtMs`へ名称を変え、同一packetの配送観測とは区別した。

`packet-delivery-01`の準備1回＋測定3回はtranscript一致・応答完了・明示終了が成功した。測定3回の同一packet受信→配送は4.4ms、10.0ms、2.4msだった。生観測は`frontend/test-results/livekit-quality/runs/packet-delivery-01/`に保存した。track初期のpacketであり、アプリの応答音声との対応は未確定のため、manifestの`media_observation_method`は`unavailable`を維持する。今回の値を応答開始・first playback・TTFAの受け入れ証拠に使わない。

レポート集計には、PCM因果境界を保存しても開始時刻＋sample数から末尾時刻を再構成してしまう不整合があった。観測済みの上下限、source sample位置、20ms以下の観測幅、時刻順序、manifestとの一致を検証し、観測した末尾の下限を集計に使うよう修正した。controlledでは未検証の`getUserMedia`完了時刻を正解時計として許可しない。旧入力時計はpilotの履歴解析に限定して残し、凍結済みWebSocket artifactを変更しない。`packet-delivery-01`の既存raw trace／manifestを変更せず、新しいvalidatorでpilot集計が完了することも確認した。応答sampleへの対応がないため、この集計から品質合格とは判定しない。

変更後の検証はFrontend単体360件、レポート検証39件、Svelte／TypeScript、Python lint、Backend型検査225 source filesが成功した。レポート検証には欠測・誤sample・非数・時刻逆転・観測幅超過・旧未相関media方式の拒否を含む。正式100試行、相槌／take-turn、障害復旧、stale提示、dogfoodの受け入れ条件は未完了である。

### 長い背景音の後のVAD開始時刻（2026-09-07）

未確定の音声候補について、PCM活動量と開始時刻が無期限に保持される不具合を修正した。確率側だけを2秒の窓に制限しても、背景音がPCM閾値を超え続けると、その数十秒後の発話が背景音の開始時刻に結び付いていた。未確定候補のPCM活動も同じ2秒の窓へ限定し、確定時点で保持範囲外の開始時刻を使わないようにした。確定済み発話の開始時刻は、その後の窓移動でも変更しない。発話の元時刻をturn判定時刻で上書きする変更ではない。

`measure-utterance-continuous-background.mjs`へ、出力先・背景音の先行秒数・比較対象detectorのsourceファイル指定を追加した。同じ実Silero確率を修正前後へ与え、WAV／モデル／両detectorのhash、背景音seed、先行時間を結果へ残す。既存出力は上書きせず、新規作成に限定した。

20秒の背景音の後に固定発話を重ねる6条件（white noise／hum／pink noise、各2音量）では、修正前の候補開始→確定は20,736〜20,832msだった。修正後は全条件2,000msとなり、背景音だけの区間での確定は0件、発話終了は各1回だった。正解末尾→VAD終了は788〜1,076ms。サーバーでのutterance確定800ms指標とは区間が異なる。結果は`frontend/test-results/vad-quality/utterance-long-background-window-01.json`へ保存した。

固定音声300件と保存済み実Silero確率の再評価は、修正前の`utterance-detector-v2-guarded.json`に対し、検出漏れ・開始遅延・早期終了・分割・終了遅延の全300件が一致した。相槌17/100、take-turn 2/100の検出漏れは残っており、合格扱いにしない。非発声音120件の実Silero診断も、誤確定9件で修正前と同じだった。これらはブラウザの全経路を通す割り込み100試行の代用ではない。

実サービスの`candidate-window-01`は準備1回＋測定3回が成功し、全試行でtranscript一致・応答完了・明示終了を確認した。Frontend単体362件とSvelte／TypeScript検査も成功した。今回の実接続は通常固定発話の回帰確認であり、長い背景音6条件はオフラインの実Silero診断である。応答sourceとfirst playbackの対応が未確定のため、TTFA合格は引き続き未証明である。

### 応答source相関で確認したSDK制約

現在のCharacter AudioTrackは複数応答で共有され、RTP frameへCoreのresponse IDは付いていない。公開SDKの[frame metadata仕様](https://docs.livekit.io/transport/media/frame-metadata/)も映像限定で、音声には未対応と明記している。Pythonの`AudioFrame.userdata`はRTPへ送られるresponse metadataとして使用できない。応答ごとのtrack分離は旧応答と新応答の混入を防ぐ候補だが、track発行後の無音やdecoderのcomfort noiseと、実際の応答PCMの先頭を区別する課題は残る。source相関が確認できるまでは、trackの初回packetや最初の非ゼロsampleを応答開始として採用しない。

### 応答ごとのAudioTrack分離（2026-09-07）

全応答が同じCharacter AudioTrackを使う構成を変更し、`ResponseAudioTracks`が応答ごとに独立したAudioSource／AudioTrackを所有する。track名の`ds-response-v1:<response_id>`をブラウザで検証し、対応する応答のrender観測だけへ割り当てる。次応答の発行前には旧trackをunpublishしてsourceをcloseする。cancelでは対応するqueueをclearしてtrackをmuteし、旧responseのPCMが新sourceへ送られないようにした。待機中のcapture、publish中断、終了との競合、破棄済みsourceへの再操作も検証した。

ブラウザは停止済み応答を同一sessionの再接続でも保持し、次応答のsegmentで古いtrackを再開しない。response IDを持たないtrackは再生経路へ接続しない。Backend／Frontendはtrack名方式を揃えて更新する。これはtrackの所有先の分離であり、decoder内のcomfort noiseやlogical segment内のPCM先頭sampleの対応を確定するものではない。既存のfirst playback値を今回の変更だけで受け入れ証拠へ昇格させない。

`response-tracks-01`の準備1回＋測定3回ではtranscript一致・応答完了・明示終了が成功し、全4回で受信trackのresponse IDが応答と一致した。続いて`response-tracks-session-01`／`response-tracks-session-02`では、同じsession・conversation・マイクstreamを維持した3往復を各1回実行した。両runとも3応答のIDが一意で、trackのresponse IDと一致し、途中の追加マイク操作0回、最後の明示終了が成功した。各入力は同じhashの合成fixtureであり、物理マイクによる手動会話や独立100試行ではない。

`response-tracks-session-02`には実際のtrack発行・解除・native source解放完了をserver monotonic traceへ記録した。発行は3件、次応答前のunpublishは2件、source closeは3件だった。1→2応答目、2→3応答目とも、旧sourceのclose完了が次trackのpublish完了より前にあった。最後のsourceはsession終了時にcloseしたため、独立したtrack unpublishイベントは記録しない。response decision→track publish完了は1.7ms、54.4ms、55.0msで、後続2回は旧trackの解放を含む。これは少数試行のtransport処理時間であり、TTFAの合否やreconnect／stale提示の100試行受け入れを示す値ではない。

検証結果はFrontend単体366件、結合97件、Backend単体2,321件成功・1件skip、LiveKit関連module20件成功。Svelte／TypeScript、Python lint、Backend型検査226 source filesも成功した。Backend全体の初回実行ではcwdが`backend/`だったためschema／Git ignore参照5件が失敗したが、規定のリポジトリ直下から再実行して解消した。外部実接続の結果は上記3 runであり、unitのskipを実接続成功として数えない。

同一session診断の再現手順は[`scripts/voice_quality/README.md`](../scripts/voice_quality/README.md)を参照する。生manifestとtraceはignoredの各runディレクトリへ保持する。PCM先頭のreceive／decode／first playback、相槌・take-turnの残る検出漏れ、reconnect／underrun／gap／stale提示、全条件の100試行・dogfood受け入れは引き続き未完了である。


## 2026-09-07: native source・Opus decode・明示出力の境界診断

`probe_native_audio_source.py`とChromium側の専用診断を追加した。実LiveKit、Python SDK 1.1.16、Chromium 149.0.7827.55で、buffer 1,000ms／0msの入力前packetと独立Opus decodeを比較した。通常conversation UIの経路は変更していない。

`native-source-render-07`の結果は次のとおり。各欄は受信packet／decode出力数で、直接入力では同数のAudioWorklet出力・全サンプル完了を確認した。

| 条件 | 入力前1秒 | 20ms入力後 | 追加1秒入力後 |
|---|---:|---:|---:|
| 標準buffer 1,000ms | 12 / 12 | 24 / 24 | 86 / 86 |
| 直接入力 0ms | 0 / 0 | 1 / 1 | 51 / 51 |

直接入力の51 packetは各960サンプル、計48,960サンプルで、供給した102個の10ms frameと一致した。RTP timestamp／sequenceの連続性、受信→decode→出力のpacket番号、最後の出力時計通過を検証器が確認した。最初のpacketの受信→独立decodeは0.8ms、decode→browser出力への時計換算は32.95〜33.35msだった。これは単発の合成tone診断であり、通常応答TTFAやp95ではない。

診断中に次の問題を確認した。

- 受信codecは`audio/red`だった。REDのprimary Opusを取り出さずにWebCodecsへ渡すとdecodeが失敗する。診断はprimaryだけをdecodeし、冗長blockによるpacket loss回復は行わない。
- `AudioData.timestamp`は指定したchunk時刻をそのまま返す識別子ではない。連続した20ms刻みへ再構成され、1usの丸めも観測した。診断はpacketごとに`flush()`でoutputを回収し、現在の入力packet番号をPCMとともに明示的に伝える。
- `native-source-render-03`では、workerの`timeOrigin`差だけの換算が画面の時計より約1,291ms早くなり、入力前snapshotより前にpacketを受信したように見えた。換算の正しさを前提にせず、往復messageからoffsetを囲む方式へ変更した。`native-source-render-07`のoffset幅は時計丸め余裕を含む0.4msだった。

失敗した`native-source-buffer-*`、`native-source-render-01`〜`05`も同じローカル結果ディレクトリに保持した。`native-source-render-06`は丸め余裕追加前の成功診断で、最終確認は`07`。時計換算ミス、RTP欠落、誤packet相関、不完全出力、出力時計未通過などを拒否する単体テスト8件が成功した。

`4ea1b4c`時点の通常会話の`RemoteMediaObserver`には`timeOrigin`のみの換算が残っていたため、既存のraw packet latencyを有効な応答遅延の証拠へ格上げしない。次の実装では因果的な時計較正に加え、最初の送信packetを欠落なく捕捉する購読準備、上限付きPCM queueの10ms pacing、segment末尾とcodec遅延の対応、RED／loss／再接続時の回復、明示PCM出力とcancelの相関を扱う必要がある。今回の独立decoderは通常再生へ未接続で、source PCM offsetの一致、音質維持、underrun 0件、全cohort受け入れは未検証である。


## 2026-09-07: 通常会話のworker時計較正

通常会話の`RemoteMediaObserver`も、workerの生monotonic時刻と10回の往復messageからoffsetの上下限を求める方式へ変更した。workerの`timeOrigin`は受信・encoded時刻の換算に使用しない。100us時計を前提とした両端各0.2msの丸め余裕を含め、幅1ms以下を記録条件とする。較正前の最初のpacket通知は保留し、1秒以内に較正できない場合や時計が矛盾する場合は`clock_calibration_failed`を残して観測transformを外す。音声frameは較正を待たずに通過する。

`firstPacketReceivedAtBoundsMs`、`firstEncodedFrameAtBoundsMs`、`workerClockOffsetBoundsMs`と`workerClockMethod: causal_message_bounds`をraw evidenceへ記録する。既存のscalar受信時刻には下限を使い、受信→配送の差分を小さく見積もらない。source受信時刻と同じpacketの配送時刻の照合は引き続きSSRC＋RTP timestampで行い、配送APIのepoch時刻が受信前・現在より未来なら欠測とする。配送時刻はdecodeしたPCM先頭との相関を証明する値ではない。

実会話pilot`causal-media-clock-01`の準備1回＋測定3回はtranscript一致、応答完了、明示終了が全件成功した。全4回でworker較正・packet受信・同一packet配送を観測し、較正幅は約0.4msだった。測定3回の受信下限→配送は約26.0ms、26.4ms、24.3ms。標準native bufferの最初のpacketには入力前の無音が含まれるため、全試行の`media_observation_method`は`unavailable`を維持し、これらを応答TTFAの合格根拠にしない。

Frontend単体テスト375件と型検査が成功した。後処理を統一した最終変更後にも、対象の時計・media observerテスト23件が成功した。実測結果は`frontend/test-results/livekit-quality/runs/causal-media-clock-01/`に保持する。


## 2026-09-07: 入力のあるframeだけを送るPCM pacing

通常会話のnative AudioSourceを`queue_size_ms=0`へ変更し、`PacedPcmSource`が最大1秒のPCM queueから10ms frameを供給する構成にした。queueの外に送信中の1 frameがあり、合成済みPCM本体はCore側にも保持する。入力前の無音を生成せず、10ms未満の端数はsegment間で保存する。応答末尾では20ms境界へのpadding＋追加20msを明示的に送り、native供給完了後に`response_completed`を配送する。paddingは元のPCMやlogical segmentの長さへ加えない。

cancel、送信taskのキャンセル、次応答、session終了は所有するpacerを止める。空き待ち中のproducerも解除し、旧PCMが新sourceへ入ることを防ぐ。native capture失敗はproducer／finishへ伝える。`first_audio_out`のtraceとcontrol観測には最初のnative capture完了時刻を保持し、長いsegmentのqueue待ちが終わった時刻を使わない。この時刻はSDKへの最初の10ms供給完了であり、最初のRTP packetの送信時刻やbrowser出力時刻とは異なる。

`paced-source-01`は固定入力のsourceStart境界を20ms以内に確定できず失敗した。失敗runを保存し、同じコードで実行した`paced-source-02`の準備1回＋測定3回はtranscript一致、応答完了、明示終了が成功した。track作成から最初のpacketまでの測定3回は約666.8ms、695.4ms、720.3msで、標準bufferの入力前packetを観測する状態から変わった。全4回で最初のnative capture、source drain、source closeを記録した。準備回を含む生成完了→最初のnative供給は5.3〜9.2msだった。入力時計の失敗を成功へ置き換えたという意味ではなく、正式な100試行の失敗率評価は未実施である。

続く`paced-source-session-01`では同じsession・conversationで3往復が成功し、追加マイク操作0回、異なる応答track、最後の明示終了を確認した。送信側のsample数は次のとおり。

| 応答 | 入力PCM | 末尾padding | native供給 | queue最大 |
|---|---:|---:|---:|---:|
| 1 | 167,422 | 1,538 | 168,960 | 48,000 |
| 2 | 120,830 | 1,090 | 121,920 | 48,000 |
| 3 | 137,726 | 1,474 | 139,200 | 48,000 |

全応答で入力＋padding＝native供給が一致した。これはnativeへ渡したsample数の検証であり、受信loss、decoderの遅延、ブラウザで再生したsource PCM位置、underrunの証明ではない。通常pilotの`media_observation_method`は引き続き`unavailable`とする。次に、購読・再生準備を送信開始前に確認する処理と、packet→decode→明示PCM出力の対応を通常経路へ組み込む必要がある。

Backend全単体テストは2,334件成功・1件skip。最終の統計追加後にはpacer、応答track、LiveKit関連moduleの34件と型検査が成功した。先行したmodule全体との混載実行では単体1件が二重clearで失敗したため、clearを冪等化してから全単体を再実行している。source完了の統計4種類は本文を含まない数値としてtraceへ記録する。


## 2026-09-07: ブラウザの受信準備を確認してPCMを供給

private契約に`response_track_ready`を追加した。ブラウザが応答trackのAudioContextをresumeし、workletと出力graphを用意してから、response ID・track SID・session世代を送る。Backendは現在のparticipant identity／SIDと世代を検証し、実際に発行したtrack SIDが一致した場合だけ送信準備済みとする。publish完了より先に届く通知は1件だけ保留し、完了後の実SIDで照合する。

LLM／TTS生成と受信準備は並行する。PCMが生成された時点で未準備なら最大3秒待ち、期限切れ・cancel・終了時はqueueをclearしてtrackをmuteする。待機解除だけでPCMを送らないよう停止状態を再確認する。旧世代・旧応答・別participantからの通知では開始しない。ブラウザ側でもworklet準備中や通知配送中の切断・購読解除・世代変更後にgraphを利用可能へ戻さない。Backend／Frontendは新private契約を揃えて更新する必要があり、旧Frontendから通知が届かない場合は音声送信待ちが期限切れになる。

`response-ready-01`の準備1回＋測定3回は全件成功した。track発行完了→準備確認は約40.133／35.719／36.888／36.813msで、全4回で準備確認が最初のnative PCM供給より前だった。同じsessionの`response-ready-session-01`でも3往復が成功し、準備確認は約36.394／29.527／30.059ms、明示終了が成功した。両runの全7応答で入力sample＋末尾padding＝native供給sampleが一致した。

単体検証では通知のresponse／SID不一致、publish完了前の通知、待機期限切れ・cancel・終了、participant／世代不一致、購読解除・切断後の非同期完了を扱う。Backend単体2,342件成功・1件skip、Frontend単体379件・結合97件、型検査と対象lintが成功した。実接続の準備確認はブラウザgraphの構築完了を表し、最初のRTP packetの無欠落、source PCM先頭との対応、実際のbrowser出力完了までは証明しない。通常pilotの`media_observation_method`は引き続き`unavailable`で、正式100試行とTTFAを含む品質受け入れは未完了である。


## 2026-09-07: 復号状態を維持するpacket相関

先行する専用診断はpacketごとに`AudioDecoder.flush()`していたが、その方法では同じOpus入力を連続して復号した場合とPCM波形が異なることを実測した。[Chromiumのdecoder実装](https://chromium.googlesource.com/chromium/src/+/main/third_party/blink/renderer/modules/webcodecs/decoder_template.cc)にもflushでEOSを送り、その後にdecoderを初期化する経路がある。仕様上のoutput回収だけを根拠に、音声の連続性が保たれるとは判断しない。

`OpusPacketDecoder`は次のpacketを投入する前に960sampleのoutput callbackを待ち、途中flushを行わない。input packet番号をPCMと対にし、decoderが再構成するtimestampを識別子へ使わない。REDのprimary取り出し、Opus TOCからの20ms長検証、48kHz mono／960sample、有限sample値、待機上限1秒、終了・失敗時の解放を共通化した。20ms以外のpacketを黙って960sampleへ置き換えない。

専用診断もこの共通コードへ切り替えた。`native-source-shared-decoder-01`では、直接入力の51 packet／48,960sampleについて、packetごとにoutputを待つ方式と一括復号の最大絶対誤差・RMS誤差がともに0だった。一方、途中flushする比較decoderは最大絶対誤差約0.18754、RMS誤差約0.09863だった。値はFloat32 PCM振幅の差であり、主観音質スコアではない。明示worklet出力も全51 packetが完了し、最後の出力時計通過を確認した。

`native-source-no-flush-01`の最初の比較はunpublish後の追加packetまで含んでいたため、snapshotの51件との同一範囲を示さない。比較対象をsnapshotまでに固定した`native-source-no-flush-02`と、その後に共通コードを使った上記runでは、同じ51件／48,960sampleに対して一致した。以前の途中flush診断はpacket数や時計の履歴として残すが、音声波形を維持した再生の証明には使わない。

通常会話の`RemoteMediaObserver`にも同じ独立decoderを組み込み、最初のRTP packetのsource／timestamp／receiveTimeを照合して、`firstPacketDecodedAtBoundsMs`と960sampleを記録する。因果的なworker時計較正を適用し、時刻逆転・別packet・欠測を拒否する。独立復号の待機queueは50 packet＋復号中1件を上限とし、復号の停滞・失敗でnative音声配送を止めない。現在の出音は引き続きnativeのgraph経路で、今回の独立PCMを通常会話の再生へ接続する処理は残っている。


実会話の`packet-decode-01`は準備1回＋測定3回が全件成功し、全4回で同一packetの受信・独立decode・native track配送を別時刻として観測した。測定3回の受信→独立decodeは約1.5／1.0／1.1msで、各960sample、worker時計較正幅約0.4msだった。raw evidenceに新しいdecode境界が残ることを確認したが、通常会話の出力PCMはまだ独立decoderから供給していないため、`media_observation_method`は`unavailable`を維持する。これらはfirst playbackやTTFAの受け入れ証拠ではない。

Frontend単体407件・結合97件、型検査、診断validatorの単体17件とPython lintが成功した。Frontendでは誤ったpacket／時計／sample数、復号timeout・異常出力・終了、復号queue超過でもnative配送が継続することを検証した。明示PCM再生とsource位置、loss／RED回復、underrun／gap／stale提示、正式100試行とdogfoodの品質受け入れは引き続き未完了である。


## 2026-09-07: 応答packetのPCMを通常会話のworkletへ出力

通常会話の出音を、独立Opus復号のFloat32 PCMを受け取る`PacketRenderer`へ接続した。native MediaStreamAudioSourceNodeから音声をコピーする経路は使わず、mutedのaudio要素はWebRTC処理を維持するために残す。Backendへの`response_track_ready`は、復号器・worker時計・AudioContext・workletが準備できてから送る。必要なブラウザAPIや復号処理が失敗した場合に、未検証の別再生経路へ切り替えて計測成功とはしない。

workletはpacket番号、RTP timestamp、packet内sample offsetとともにPCMを受け取り、実際に出力バッファへ書いた区間だけを数値で通知する。入力前のゼロ出力を応答sampleとして数えない。初回に60msのbufferを確保し、待機PCMは最大48,000sample。停止ではqueueを破棄し、旧応答の遅着PCMとresume通知でも再開しない。元の発話開始時刻と停止応答の相関は維持する。

`PacketOutputTracker`は、render callbackが来ただけでは再生済みとせず、`getOutputTimestamp()`のcontext clockが当該区間の末尾を通過するまで保留する。その時計から先頭frameのclient monotonic時刻を求める。最初のpacketの受信・復号・出力frame・出力時刻・観測時刻・出力時計の生値を`packet_playback_observation`へ保存し、Backendのvalidatorが時計通過と対応を再計算する。first playbackの通知とpilot cycleにも同じ確定値を使い、callback到着時刻や後から取得した時計へ置き換えない。

`packet-playback-01`の準備1回＋測定3回は成功した。このrunは出力時計の生値追加前の診断である。最終形式の`packet-playback-02`も全4回成功し、保存結果だけを使う再検証が通った。測定3回の値は次のとおり。

| 測定 | 同一packet受信→独立復号 | 独立復号→最初のbrowser出力 |
|---|---:|---:|
| 1 | 1.5ms | 95.468ms |
| 2 | 1.7ms | 102.691ms |
| 3 | 1.4ms | 93.697ms |

各初回出力区間は64sampleで、出力clockはその末尾を通過していた。全体のpacketは960sampleだが、worklet quantum境界の途中から開始するため初回通知が64sampleになる。first playbackはdecoded応答packetの先頭sampleをbrowserへ出力した時刻で、音量閾値を超えた時刻や物理スピーカーの音響到達時刻ではない。

`packet-playback-session-01`では同じsession・conversationの3往復が成功し、全応答でtranscriptとtrackの応答IDが一致、追加マイク操作0回、最後の明示終了を確認した。`packet-playback-02/pilot-aggregate-01.json`へのpilot集計は出力時計の再検証、schema検証、匿名性検査を通過した。保存先はignoredの各runディレクトリである。

Frontend全単体418件・結合97件と型検査が成功し、その後に追加したRoom境界の旧PCM破棄テストを含む16件も成功した。Backend report単体49件・型検査227 source files・対象lintが成功した。途中のFrontend結合3件は、helperがRoom型を参照して不要なJSON依存まで探索したため失敗し、再生証跡の型へ参照を限定して解消した。別の部分実行ではcwdをリポジトリrootにしたため境界検査が失敗したが、Frontendディレクトリからの全実行で成功を確認している。

この変更で相関できたのは受信packetのPCMとbrowser出力である。codecのlookaheadや初回packet欠落を含めた元のsource PCM offset、logical segment末尾まで聞こえたかの厳密なprefixは未確定のため、`sourcePcmOffsetVerified: false`、`media_observation_method: unavailable`を維持する。末尾までの再生完了通知、途中の空きと正常な末尾の区別、loss／RED回復、再接続、実割り込みのstale提示、全100試行とdogfood受け入れは未完了である。今回の少数成功やunitの停止検証を、音切れ0件・stale提示0件・TTFA品質合格へ拡大して扱わない。

## 2026-09-07: 応答末尾の再生確認と音切れの集計

BackendはPCM供給を終えた時点で、入力・padding・native供給済みのsample総数をprivate `response_audio_finished`へ送る。OpusのDTXは明示的に無効にする。Frontendは、この通知だけで再生完了とせず、連続するRTP timestamp、全packetのsample数、全出力区間の出力時計通過を確認してから完了を通知する。UIも音声がある応答ではこの完了を待つ。正常な末尾の後のゼロ出力は音切れに含めず、先頭から末尾までの出力区間の間だけをgapとして数える。末尾packetの欠測を時間経過で補完しない。

`complete-playback-01`の準備1回＋独立測定3回は全sampleの再生完了を確認した。測定3回のgap合計は0／9.333／2.667ms、gap件数は0／6／1件、最大連続gapはいずれも0または2.667msだった。したがって制御測定のunderrun 0件は未達である。送信traceのsample総数と出力時計を再検証した[匿名artifact](artifacts/livekit-pilot-2026-09-07-playback-completion.json)はschema検証・匿名性検査を通過した。

`playback_continuity`は、同じAudioContext上で「先頭frameから全sampleを連続出力した場合の末尾」と「実際の末尾」を比較する。追加指標`playback_gap_total_ms`、`playback_gap_maximum_ms`、`playback_underrun_count`、`playback_duration_ms`は、検証済みの完了証跡から生成する。warm-upを除外し、音切れがある試行も観測値として残す。計測成功と品質合格は区別する。packet全体の出力完了は確認できたが、codec lookaheadを含む元PCM offsetとlogical segmentの厳密なplayed prefixは未確定のままである。

同一session試験では固定の1.5秒待機を外し、実際の再生完了と入力fixtureの終了を待って次の発話へ進む。`complete-playback-session-01`は3往復目の応答開始待ちで失敗し、再試行`complete-playback-session-02`は3往復と明示終了が成功した。これだけでは断続的な失敗が解消したとは判断しない。

失敗時に本文なしのCoreイベント（認識文字数を含む）、マイク状態、再生完了、固定のtransport失敗箇所を保存するようにした。`source-timing-session-01`は2往復目の応答途中で切断、`source-timing-session-02`は3往復目で認識文字数0の`utterance_finalized`に続いて`response_failed`となった。後者ではtransport失敗は記録されず、空のSTT結果による応答失敗まで原因を絞れた。STT入力のsample数・peak・RMS・振幅200超のsample数を追加し、波形を保存せず入力欠落を切り分ける。

native capture待ち、入力待ち、送信予定時刻の繰り下げも数値で計測する。修正前の`source-timing-session-01`の最初の応答では入力待ち0ms、送信予定の繰り下げ約58msだった。従来の毎回の繰り下げをやめ、短い遅延は次周期で回収する。長い停止後の追い付きは20ms（1 Opus packet分）に制限する。`paced-recovery-session-01`の最初の4往復はgap 0msで末尾まで確認したが、5往復目でrenderer経路の失敗による切断が発生した。10往復試験全体は失敗であり、送信周期の修正だけで連続会話を合格とはしない。

Backend単体2379件成功・1件skip、対象Python型検査とFrontend型検査が成功した。再接続経路への診断追加で発生したFrontendの型エラー2件・単体失敗3件は修正し、関連単体61件の成功を確認した。再生切断の具体原因、空のSTT入力／結果の原因、loss回復、全cohortの100試行とdogfood受け入れは残っている。


`paced-recovery-session-02`は8往復の末尾まで確認後、9往復目で`invalid packet render interval`となった。既知の内部エラー名と、不一致時のpacket番号・sample offset・出力frameだけを追加保存する。切断や`response_failed`を確認した試行は、残りの長いタイムアウトを待たず診断を保存して終了する。

最終の`render-interval-session-01`は、同一session・conversationの10往復、transcript一致、全応答の末尾出力、追加操作0回、明示終了が成功した。gap合計は順に2.667／0／40／0／0／0／0／0／0／0msで、品質のunderrun 0件は未達。断続的な再生区間不一致と空の認識結果が解消したとは判断しない。最終差分のFrontend全単体425件・結合97件と型検査は成功した。
