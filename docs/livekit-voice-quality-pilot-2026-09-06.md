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
