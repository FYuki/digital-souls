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


## 2026-09-07: ブラウザ音声時計の照合とSTTの先頭静音

`render-interval-session-02`は3往復後に失敗した。packet番号とpacket内offsetは正しく進んでいたが、workletのglobal `currentFrame`が同じ128sample区間を繰り返した。利用中のChromium 149.0.7827.55の[BaseAudioContext実装](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.55/third_party/blink/renderer/modules/webaudio/base_audio_context.cc)では、graph lockの取得に失敗するとworklet global frameの更新を見送る。[音声destination](https://chromium.googlesource.com/chromium/src/+/refs/tags/149.0.7827.55/third_party/blink/renderer/modules/webaudio/realtime_audio_destination_handler.cc)のsample counterはその場合も進む。

`RenderQuantumClock`は実際の出力quantum数からframeを追跡し、global値が止まっている間の証跡を保留する。次に更新されたglobal値と一致した場合だけ確定する。PCMの重複再生やsample破棄で帳尻を合わせず、不一致・長時間の未照合では失敗する。独立したAudioParam automationの時間軸と比較する`probe_render_clock.mjs`を追加した。実ブラウザのgraph変更負荷試験`render-clock-01.json`では、7,424 quantumまでのsnapshotでglobal更新停滞210回、counterとautomationの不一致0回、最大誤差0.045655sample未満を確認した。終了時のAudioContext closeも確認した。

`reconciled-clock-session-01`は再生区間エラーがなく、2往復目でSTT結果が空になった。入力には49,600sample、peak 10,763、RMS約1,776があり、入力全体の欠落ではなかった。先頭には約1.6秒の非常に小さい信号があった。同じ固定音声に0〜2,000msの先頭無音を加える実Whisper診断では、2,000msの2回がともに空の認識結果になり、それ以外の8回は一致した。

STT境界に`prepare_stt_audio`を追加し、PCM16の振幅16を初めて超える位置より前の320msと、その後の全sampleを保持する。長いほぼ無音の先頭だけを短縮し、文中の休止と末尾は切らない。全体が小音量の場合は入力を維持する。通常STTと割り込みの先行STTに適用し、モデルは変更しない。同じ先頭無音5条件×2回の準備後診断は10回ともtranscript一致となった。`probe_stt_preroll.py`はfixtureのhashを確認し、本文・音声を保存せず件数と数値結果を残す。

`prepared-stt-clock-session-01`は10往復と明示終了が成功した。`prepared-stt-clock-pilot-01`も準備1回＋独立測定3回が成功し、送信sample数・全packet出力・出力時計を再検証した[匿名artifact](artifacts/livekit-pilot-2026-09-07-reconciled-clock.json)がschema・匿名性検査を通過した。測定3回のgap合計は10.667／1.333／2.667msで、制御測定のunderrun 0件は未達である。

Backend全単体は2,394件成功・1件skip。時計とrendererの関連単体26件、STTと音声bridgeの関連単体48件、report単体71件、対象型検査が成功した。今回の少数試行は、全cohortの100試行、dogfood品質、loss回復、実割り込みのstale提示を証明するものではない。


## 2026-09-07: 実出力packetの受信・復号時刻をtraceへ相関

従来の`media_observation_method: unavailable`は、元source PCM offsetと応答packetの対応を同じ未確認事項として扱っていた。現在は応答専用track、受信準備確認、同じOpus packetの状態付き復号、workletへのPCM供給、出力時計通過を通常会話で確認できる。元PCMのcodec lookahead／logical segmentの厳密なplayed prefixを確定せずとも、ユーザーが採用した「decoded応答packetの先頭sampleをbrowserへ出力した時刻」とそのpacketの受信・復号は相関できる。

Roomはtrack名だけで相関せず、最初のpacketのRTP timestampと受信・復号時刻が保存済みmedia観測に一致し、そのPCMの出力時計が通過した場合に限り、`client_track_received`・`client_encoded_received`・`client_audio_decoded`を元の観測時刻で送る。停止・購読解除後や不一致では送らない。Playwrightもnative配送時刻の代わりに、実際にworkletへ供給した独立decoderのoutput時刻を使う。

集計methodを`response_track_stateful_opus_worklet_output`とし、trace、試行内時刻、生のpacket出力証跡を照合する。正式controlled測定では古いnative配送methodやpacket証跡の欠落を拒否する。Room境界20件とreport75件の単体テストで、出力前、不一致、停止・購読解除後、旧method、配送時刻の代用を含めて検証した。

`correlated-packet-pilot-01`の準備1回＋独立測定3回は全件成功し、[匿名artifact](artifacts/livekit-pilot-2026-09-07-correlated-packet.json)のschema・匿名性・生値照合が成功した。測定3回の`client_decode_latency` p95は1ms、`decoded_to_playback_latency` p95は99.7msで、両指標は3/3実測・欠測0件となった。TTFA p95は2,212.53ms、utterance確定p95は539.14msで、TTFAの絶対目標は未達である。

## 2026-09-07: 同じPCM波形を保つTTS変換の高速化

標準のPCM16 monoをPythonでsampleごとにbytesから変換し、24kHz→48kHzを毎sample一般式で補間する処理を調べた。合成PCM 72,000sampleの7回診断では、元処理の中央値83.985msに対し、候補処理は7.421msだった。出力143,999sampleの差分は0件。この診断はCPU処理時間であり、TTSサービスの合成時間や音切れ改善の直接証明ではない。

PCM16 monoは一括unpackし、2倍sample rateでは元sampleと隣接sampleの中点を交互に配置する。既存の偶数丸め、末尾sample数を維持し、他の入力形式・sample rateは従来経路を使う。最終実装にはPCM16範囲検査も含む。全PCM16値と中点の丸め、異なるsample rate、不完全なsampleの拒否を含むadapter単体22件が成功した。

`fast-pcm-pilot-01`の準備1回＋独立測定3回は全件成功し、4回ともgap 0sampleだった。[匿名artifact](artifacts/livekit-pilot-2026-09-07-fast-pcm.json)のschema・匿名性・生値照合も成功した。測定3回のTTFAは約1,969／2,098／2,150ms、保守的なfixture境界を使う集計p95は2,144.08msで、目標2,000msには届かない。少数試行であるため、制御測定100試行のunderrun 0件を達成したとは判断しない。

最終差分のBackend全単体2,403件成功・1件skip、Frontend全単体439件・結合97件、Frontend型検査と対象Python型検査・lintが成功した。実サービスは既存の共有推論を変更せず、独立したintegration-voice data rootを使用した。正式100試行、全割り込み・再接続cohortとdogfood品質の受け入れは残っている。


最終実装を旧コミット`bdf6235`と同じ合成PCMで再比較した`pcm-normalization-cpu-02.json`では、7回の中央値が83.095ms→8.116msとなり、出力143,999sampleの差分は0件だった。全ての負・正の0.5境界を使うよう補間テストを強化し、関連22件の成功を再確認した。

`fast-pcm-session-01`は同一session・conversationの10往復、全応答の再生末尾、transcript一致、追加操作0回、明示終了が成功した。10回ともgap 0sample、失敗診断なしだった。正式100試行の安定性や、他cohortの品質まで拡大して合格とは扱わない。

Frontendのproduction buildも成功した。大きいbundleに対するViteの警告は残るが、buildエラーはなかった。


## 正式100試行の実行入口

`run_pilot.py --controlled --scheduled-fixture`は、少数pilotの設定を除去し、5 warm-up＋独立100 sessionのspecを選ぶ。`--trials`との同時指定、固定PCM時計なし、連続session診断との混在を拒否する。出力は引き続き新しいrun専用data rootへ保存し、既存runを上書きしない。集計には`python -m app.livekit_pilot_report --scope controlled`を使う。100試行の収集成功と、TTFAや音切れの品質合格は分けて判定する。入口のscope・分母・環境選択を含む関連単体23件が成功した。


## 2026-09-07: 高速PCM・実packet相関での正式通常100試行

`fast-pcm-controlled-01`をコミット`0506eda`の実装で実行した。14.3分で準備5回＋独立100試行が終了し、transcript一致、応答完了、全sampleの出力時計通過、明示終了が105/105成功した。session／conversationは各105個で重複なし、各試行の空の会話・記憶状態と設定hashは一致した。共有サービスの設定とdogfoodデータは変更していない。`think:false`、RAGなしという実験条件であり、元の生成設定と同じ人格・記憶応答品質の証明ではない。

[正式匿名artifact](artifacts/livekit-controlled-2026-09-07-fast-pcm.json)はcontrolled validator、schema、匿名性検査を通過した。測定100回の出力sample総数は16,996,800、gap合計・最大gap・underrunはすべて0、処理失敗0/100だった。従来の計測方式の100試行を流用した結果ではない。

| 指標 | p50 | p95 | 確認結果 |
|---|---:|---:|---|
| TTFA | 2,120.60ms | 2,186.72ms | 絶対目標2,000msは未達、相対条件は達成 |
| utterance確定 | 536.49ms | 602.23ms | 絶対目標800ms以内 |
| first text | 367.64ms | 381.20ms | 相対条件は達成 |
| LLM完了 | 470.26ms | 482.21ms | 相対条件は達成 |
| client受信→実再生 | 100ms | 104ms | baseline＋許容幅の約81.20msを超過 |
| client受信→復号 | 1ms | 2ms | 100/100実測、欠測0 |
| client復号→実再生 | 98ms | 102ms | 100/100実測、欠測0 |

[自動比較結果](artifacts/livekit-controlled-2026-09-07-fast-pcm-evaluation.json)は`latency_only`として不合格を記録した。TTFAの超過に加え、client再生待ち、STT開始待ち、response decision、utterance確定の相対比較が不合格になった。後者の区間にはWebSocket／LiveKitの確定段階や観測位置の相違があり、名前の一致だけで同じ境界と判断しないよう追加監査が必要である。保存した不合格結果を合格へ書き換えない。

割り込みのlocal stop／turn decision／cancel、VAD冒頭・終了境界、stale、reconnect、manual operationsはこの通常応答artifactで欠測が残る。resourceとnetworkも専用観測の取り込みが未完了である。割り込み・再接続のcohort、VAD境界計測、dogfood品質の受け入れは完了していない。

途中の数値診断では、STT処理中央値約401ms、STT完了→LLM開始約15ms、LLM開始→first token約355ms、最初のTTS生成約308ms、VAD通知→STT開始約156msだった。Ollamaの生成prompt評価中央値は約38msに対して`load_duration`中央値は約274ms。v0.32.5の[ChatHandler](https://github.com/ollama/ollama/blob/v0.32.5/server/routes.go)・[scheduler](https://github.com/ollama/ollama/blob/v0.32.5/server/sched.go)では、モデル確認とrunner取得までがload値に含まれる。重みの再ロードや純粋なqueue待ちへ断定できない。共有Ollamaを変更せず、残る時間の切り分け対象とする。


## 2026-09-07: 再生の先行bufferを32msへ短縮する候補

60ms設定の正式100試行ではgap 0件だったが、client受信→実再生p95 104msが相対上限約81.20msを超えた。先行bufferを32ms（48kHzで1,536sample）へ短縮し、入力前のゼロを応答sampleへ含めないこと、全PCMの出力、停止後の破棄、global時計の照合を含む関連39テストが成功した。

`buffer32-pilot-01`の準備1回＋独立10試行は全件成功、gap 0sampleだった。[匿名artifact](artifacts/livekit-pilot-2026-09-07-buffer32.json)のschema・匿名性・出力時計照合が成功した。測定10回のclient受信→実再生p95は75.55ms、復号→実再生p95は74msで、少数試行では相対上限以内となった。TTFA p95は2,138.025msで未達。32ms設定の正式100試行の安定性はまだ確認していない。

## 2026-09-07: STTの待機後遅延と無音による準備

同じ固定fixtureを元PCM／32kbps Opus経由／末尾静音320msにした12回の診断ではtranscriptが全件一致した。先頭の1回は約445ms、その後は約109〜123msだった。新しいHTTP clientを繰り返し作った追加診断でも、最初の1回の後は概ね約116〜160msとなり、HTTP接続を新しくするだけでは毎回の遅延を再現しなかった。診断用の`/health`取得は404だったため、readiness成功の証拠には使わない。

[待機時間の診断](artifacts/stt-idle-2026-09-07.json)では、同じPCMで0.5秒待機後が約137ms、1秒後が約174ms、2秒後が約300ms、4秒後が約447msとなった。4秒待機後に100msの無音PCMを先に処理し、400ms後に本来の音声を渡す2回は約123／156msだった。無音の準備自体には約436〜450msかかり、総計算時間を減らした測定ではない。全8回でtranscript一致。先頭の`idle_seconds: 0`は診断内で追加待機していないという意味で、測定前のidle時間は制御していない。

GPU clockの変化など、待機後に遅くなるハードウェア側の具体原因は未確定である。モデルやユーザー音声を変更せず、発話末尾の待機と準備処理を重ねる余地を示す診断として扱う。通常会話への準備処理は未実装で、共有STTの同時実行上限、前景の認識との競合、キャンセルと終了の扱いを設計する必要がある。`probe_stt_idle.py`を追加し、固定fixtureのhash確認、上書き拒否、本文・音声を含まない数値保存で再実行できるようにした。

追加したコマンドの再実行`stt-idle-02`も8/8でtranscript一致した。4秒待機後約409ms、無音で準備後約125／126msとなり、待機後遅延の軽減が再現した。2秒待機後は約408msであり、idle時間から遅延を一意に予測できる測定ではない。


## 2026-09-07: 発話終了待ちとSTT準備を重ねる実装

確認済みの通常発話で300ms連続の静音PCMを受信したら、100msの無音だけをWhisperへ一度送って準備する。VAD通知前に保持したPCMも静音時間の判定に使う。発話終了の条件、ユーザー音声の末尾、Whisperのモデルと推論設定は変更しない。割り込みでは既存の冒頭認識と競合しないよう準備を省く。認識中・容量超過・発話確定・session終了時も新しい準備を投入しない。

Whisper adapterは準備と通常認識の同時投入を防ぎ、通常認識は準備中だけ待つ。通常認識同士の既存の同時実行上限は維持する。待機中にキャンセルされたユーザー音声は準備終了後にも送信しない。準備のawaitがキャンセルされても、実HTTP処理が終わるまでは予約を保持する。clientだけ短いtimeoutで打ち切るとserviceに推論が残るため、既存のservice timeoutを上回るHTTP timeoutを利用する。サービス異常時には準備待ちも長くなり得る。準備は追加の計算を伴い、総計算量を減らす変更ではない。

準備の開始・終了と成功値は`stt_preparation`の別stageへ記録し、準備を待つ必要がある場合の時間は本来の`stt_started → stt_completed`から除外しない。awaitのキャンセルは実処理完了と区別して記録する。準備失敗を通常認識の失敗件数へ混ぜない。競合、キャンセル、任意capabilityがないadapter、準備の失敗、静音中に発話が再開した場合、session終了を含む関連単体87件とRuffが成功した。

`stt-preparation-pilot-01`は準備1回＋独立10試行が成功し、11/11で明示終了を確認した。[通常応答artifact](artifacts/livekit-pilot-2026-09-07-stt-preparation.json)はschema・匿名性・実出力時計の検証を通過した。測定10回のTTFA p50は1,826.70ms、p95は1,918.73ms。utterance確定p95は296.16ms、client受信→実再生p95は73.10msだった。処理失敗、gap、underrunは0件だった。

[準備の数値診断](artifacts/stt-preparation-pilot-2026-09-07.json)では測定10回すべてで準備が通常認識の開始前に完了した。STT処理p50は121.21ms、p95は138.92msとなった。少数試行での目標達成であり、正式100試行の合格とはしない。引き続き`think:false`、RAGなしの実験条件であり、人格・記憶品質、割り込み・再接続・dogfood品質の受け入れは別途必要である。


Backendの単体・結合を同一processで通した検査は3,847件成功、1件skip、RAGテスト1件失敗だった。失敗はSTT処理を通らないHTTP会話で、単独では成功し、Chroma storeの単体テストを先に1件実行すると再現した。テスト用のmodule再importが親packageの属性を復元せず、後続のmonkeypatch先と実行moduleが食い違っていた。親属性もteardownで復元する変更後、Chroma・runtime path・HTTP chatの関連67件が成功した。RAG本体や検索条件は変更していない。


## 2026-09-07: STT準備・32ms bufferでの正式通常100試行

コミット`dc0c9fa`の実装で`stt-preparation-controlled-01`を実行し、13.8分で準備5回＋独立100試行が成功した。105/105でtranscript一致、応答全sampleの出力時計通過、明示終了を確認した。測定100回の出力sample総数は17,154,240、処理失敗・gap合計・最大gap・underrunは0件だった。environment reportは`completed / complete`、所有Frontend・Backendのteardownも完了した。

[正式匿名artifact](artifacts/livekit-controlled-2026-09-07-stt-preparation.json)はcontrolled validator、schema、匿名性検査を通過した。

| 指標 | p50 | p95 | 確認結果 |
|---|---:|---:|---|
| TTFA | 1,825.55ms | 1,888.305ms | 絶対目標2,000ms以内、相対条件も達成。p50改善目安1,000msには未達 |
| utterance確定 | 276.15ms | 287.92ms | 絶対目標800ms以内、現行自動比較の相対条件は不合格 |
| STT処理 | 120.46ms | 131.08ms | 準備待ちがある場合も含む |
| first text | 367.54ms | 380.79ms | 相対条件は達成 |
| LLM完了 | 466.08ms | 484.41ms | 相対条件は達成 |
| client受信→実再生 | 70ms | 75ms | 相対上限約81.20ms以内 |

[準備の診断](artifacts/stt-preparation-controlled-2026-09-07.json)では測定100回すべてで準備が成功し、98回は通常STT開始前に完了した。残る2回の待機もSTT処理時間に含めた。準備自体のp50は412.36ms、p95は476.23msで、追加計算を発話終了待ちと重ねた結果である。

[自動比較結果](artifacts/livekit-controlled-2026-09-07-stt-preparation-evaluation.json)は`latency_only`として全体不合格を維持している。割り込み4指標とVAD境界2指標の欠測、`response_decision`・`stt_start_latency`・`utterance_finalized`の相対条件が残る。コード確認ではWebSocketのutterance確定はブラウザで録音PCMを取り出した時点、LiveKitの同名eventはCoreのSTT・turn分類後であり、観測境界が一致していない。STT開始待ちもWebSocketは完成したPCMの受信後、LiveKitは連続microphone PCMの最初の受信後となっている。比較の境界監査と、揃えた区間の追加計測が必要である。保存済みの不合格結果を合格へ書き換えない。

この100試行も`think:false`・RAGなしのintegration-voice条件である。生成設定を変更した際の人格・記憶応答品質、相槌・take-turn・reconnect・dogfood品質、resource／networkの受け入れは完了していない。Backend・環境の型検査は228ファイルで成功した。


Chromaテストの復元修正後、Backend単体・結合の全体再検証は3,848件成功、1件skipで完了した。元の作業ディレクトリは`epic/182-tool-foundation`のまま変更なしだった。

resource計測の追加確認では、run reportが所有を記録するBackendコンテナのDocker statsからCPU累積時間とメモリ使用量を読み取れた。1回のメモリ使用量は255,021,056bytesだったが、これはcontainerに課金されるメモリ量のsnapshotであり、全試行のRSSやpeakではない。現在のaggregateへ補完していない。GPUは既存observerがhost値を保存しているがschemaへの取り込みは未完了。WebRTCの送受信・lossは、使用中のLiveKit SDKで公開されているsender／receiver statsから集める必要がある。既存manifestのCPUはPlaywright worker単体の値であり、Backendやbrowserを含むprocess tree値へ読み替えない。


## 2026-09-07: 資源と音声RTP統計を匿名artifactへ取り込む

正常応答の全PCM出力後に、ブラウザの`RTCRtpSender.getStats()`と`RTCRtpReceiver.getStats()`から音声RTPの数値を取得する。送信は同じsenderの前回応答snapshotとの差分、受信は応答専用trackの累積値とする。SDKの受信stats wrapperはpacket数・lossを返していなかったため、公開されているreceiverの標準APIを直接使った。stats ID・SSRC・IPは出力しない。未取得、不正値、巻戻り、曖昧な複数stream、2秒timeoutを理由付き欠測として残す。

CPU・メモリはrun reportが所有するBackendコンテナから約0.5秒間隔で読み、`integration-voice`・`test`・当該data rootの一致を確認する。CPUは連続sample間のCPU累積時間差分／観測時間で、1 coreを100%とする。メモリはsampled container charged bytesの最大値であり、RSSではない。GPUは共有hostの最大device使用率と全device使用メモリ合計の最大値を低頻度sampleから集計する。BackendとGPUの観測範囲を混同せず、sample数・区間長・欠測理由も保存する。schemaへの追加は任意項目とし、凍結済みWebSocket artifactを変更しない。

`resource-network-pilot-01`、環境照合追加後の`resource-network-pilot-02`はそれぞれ準備1回＋独立3試行が成功した。[最初のartifact](artifacts/livekit-pilot-2026-09-07-resource-network.json)と[最終版のartifact](artifacts/livekit-pilot-2026-09-07-resource-network-verified.json)はschema・匿名性・出力時計の検証を通過した。最終版では以下を記録した。

| 診断 | 値・範囲 |
|---|---|
| Backend CPU | 平均40.44%、67区間・約34.80秒 |
| Backendメモリ | sample最大150,200,320bytes、68sample |
| GPU | 最大device使用率27%、全device合計使用量のsample最大7,889,485,824bytes、8sample |
| 測定3回の送信RTP payload | 39,996bytes |
| 測定3回の受信RTP payload | 115,684bytes、497packet |
| 下り応答trackのloss | 0件、3/3で取得、欠測0 |
| TTFA | p95 1,878.24ms。3試行の診断値 |

資源観測はwarm-up・起動も含むrun中の値で、通信量はwarm-upを除外する。Backend生成前の4sampleは`owned_backend_not_running`として保存した。処理失敗・gap・underrunは0件だった。この少数runの資源・通信量を、既存の正式100試行へ補完しない。

通信量はRTP payloadで、header・padding・UDP/IP・signaling・DataChannel全体のwire量ではない。lossの範囲はbrowser下り応答trackであり、上りの損失を0と主張しない。正常再生完了時のsnapshotだけでは、障害中や再接続のloss観測を証明できない。[W3C WebRTC stats](https://www.w3.org/TR/webrtc-stats/#dom-rtcreceivedrtpstreamstats-packetslost)に従い負の累積lossも認め、別trackの正のlossと相殺せず発生件数を残す。

検証はBackend単体・結合3,867件成功・1件skip、Frontend単体450件・結合97件成功、Backend・環境の型検査230ファイル、Frontend型検査、Ruff、production buildが成功した。buildには既存の大きいbundle警告がある。


`resource-network-session-02`では同一session・conversationで3往復し、3つの異なる応答trackについて最初のpacketと全sampleの出力時計、Backend送信総数を照合した。[連続試験の匿名結果](artifacts/livekit-session-2026-09-07-resource-network.json)に、gap・underrun 0、送信RTP payload 35,855bytes、受信101,713bytes、下りloss 0/440packetを保存した。Backend CPU平均20.96%、sampled memory最大140,902,400bytes、共有GPU使用率最大81%も範囲・観測数付きで記録した。Playwrightは1件成功・unexpected 0、明示的session終了を確認した。

先行する`resource-network-session-01`では試験自体は成功したが、manifestに最初のpacketの出力時計を保存しておらず、厳密な再生照合が失敗した。完了時の値から合成せず、診断exportを修正して別runで再測定した。追加操作0回はこの時点では診断コードの固定値であり、操作observerによる計測の証明には使わない。通常aggregateへの取り込みと実操作数の観測は引き続き必要である。


## 2026-09-07: 追加操作回数を実UIから観測する

診断の固定値`additional_user_control_actions: 0`を除去し、documentのcapture listenerでclick activationを計測する。開始ボタンの処理完了後に一度だけ観測を開始し、応答音声の全sample出力後・明示終了操作前にsnapshotを保存する。キーボードによるbutton activationもclickとして数える。ページのクリック全体を保守的に数え、要素の文言・入力本文は保存しない。会話中に必要なアプリUI操作を対象とし、OS側の操作の観測とは主張しない。

集計器は観測開始がfixtureの実発話開始より前であり、観測末尾が再生完了の時計通過より後であること、activation列の時刻順・範囲を検証する。独立試行では準備分を除外して`manual_operations`へ取り込み、記録がない試行を0回へ補完しない。PCM境界を観測しない旧pilotではこの指標は欠測のままにする。

- [独立3試行](artifacts/livekit-pilot-2026-09-07-user-controls.json): 準備1回＋測定3回成功。`manual_operations`取得3/3、欠測0、p50／p95とも0回。TTFA p95 1,836.04msは少数試行の診断値。
- [同一sessionの3往復](artifacts/livekit-session-2026-09-07-user-controls.json): 同一conversation・3応答trackの全sample出力、明示終了を確認。約21.95秒の同じ観測開始点と累積activation列を照合し、追加操作0回。gap・underrunも全応答0。各snapshotの累積値を合計して二重計上せず、最後の値をsession値とする。

実Chromiumの空ページで開始clickを除外し、続くマウスclickとEnter activationが2回になること、観測途中のresetを拒否することも確認した。関連Backendテスト90件（準備除外、非ゼロ、欠測、範囲不正を含む）、Backend・環境の型検査230ファイル、Frontend型検査、Ruffは成功した。両runのPlaywrightはunexpected 0、所有Frontend・Backendコンテナの削除を確認した。共有推論サービスは停止していない。

この3往復は追加操作条件の実接続証拠であり、相槌・割り込み・再接続・dogfood品質や正式100試行全体の合格を示すものではない。


## 2026-09-07: 短いtake-turnを連続した高確率で確認する

従来の確定条件は、2秒の窓に音声確率0.3以上が300ms必要で、Silero legacyでは96ms frameを4つ必要とした。見逃したtake-turn 2件では3 frameまでしか揃っていなかった。単純に250msへ短縮した候補は見逃しを0件にした一方、非発声音の誤確定を9/120から11/120へ増やしたため採用しなかった。

通常の300ms条件を維持し、短い発話についてだけ「0.3以上が250ms以上、かつ直近で0.5以上が192ms連続」を代替条件に加えた。音量による最低活動時間、2秒の観測窓、文中無音600msと確率による終了待ち、元の開始時刻の保持は維持する。連続高確率のカウンターは必要時間で上限を設け、低確率やresetで切る。

[同じfixtureによる比較](artifacts/vad-stable-confirmation-2026-09-07.json)では、take-turnの未検出は2/100から0/100となり、冒頭100ms超の欠落・早期終了・分割も0/100だった。相槌の未検出17/100は残り、文中無音は未検出・早期終了・分割0/100のまま。これは保存済み実Silero確率とPCMを用いた検出器の診断であり、実ブラウザの割り込み見逃し率ではない。

非発声音120件の誤確定は9件で、試行ごとの新しい誤確定は0件。無音10件は同じPCM hashを持つため、unique hashは111件である。hashで重複排除せず、生成順とhash・種類の一致を照合して120件を対応付けた。20秒の背景音後に発話する6条件も、発話前の誤確定0、発話終了1回、確定時の開始点は保持窓2秒以内だった。

変更後の[実音声pilot](artifacts/livekit-pilot-2026-09-07-stable-vad.json)は準備1回＋独立3試行成功。transcript一致、全sample出力、明示終了、所有Frontend・Backendコンテナ削除を確認した。gap・underrun・追加操作は0件。TTFA p95 1,822.11msは`think:false`・RAGなしの少数診断値である。正式100試行はこのVAD変更後には再測定していない。

Frontend単体453件・結合97件、型検査が成功した。非連続の確率ピーク、短い高確率1 frame、resetでの持越しを回帰検証した。相槌の検出と分類、全stackの相槌／take-turn各100試行、VAD境界の通常aggregateへの取り込みは引き続き未完了である。


## 2026-09-07: 実再生中のtake-turnとVAD未確定を観測する

別の固定fixtureを同じマイクstreamへ送る実接続診断を追加した。初期応答の実出力開始、対応するaudio graph、再生完了前であることを確認してからv2のラベル付き音声を投入する。全件で独立sessionを使い、旧応答・割り込み発話・判定・ローカル停止・キャンセルを相関する。失敗段階とcleanupを含めて全試行を保存し、全件成功しなければ試験を失敗にする。

`labeled-take-turn-01`と数値VAD observer追加後の`labeled-take-turn-02`は、take-turn先頭4件を各1回実行し、**どちらも2/4成功・2/4未判定で不合格**だった。全8件で再生中の音声投入と明示session終了を確認し、両runの所有Frontend・Backendコンテナ削除も確認した。失敗したraw runは保持した。[匿名比較結果](artifacts/livekit-interruption-diagnostic-2026-09-07.json)の各latencyは分母4・取得2・欠測2とし、未判定を分母から取り除いていない。

2回目の取得2件では、元の発話開始→local stop p95 709ms、元の発話開始→turn decision受信 p95 709ms、serverのdecision→cancel p95 5.06ms、元の発話開始→cancel確認 p95 733.75msだった。同じsession／割り込みutterance／旧responseのtrace、Browserの停止時刻、cancel通知を照合した値で、各100件の受け入れ値ではない。全4件の見逃し率は50%で未達である。

未判定2件ではPCM活動をRMSで観測したが、ブラウザ内Sileroの最大音声確率は約0.117と0.242で、VADはcandidateの後にmisfireとなった。元の単独診断では同じfixtureの最大確率は約0.820と0.580だった。連続マイクの前状態・開始位相・ブラウザ入力経路のどれが差を作るかはまだ分離できておらず、確率閾値を下げただけで解決したとはしない。前段のVAD未確定へ原因を絞れたが、修正と再検証が必要である。

Frontend単体454件・結合97件、型検査、診断入口のBackend29件、Ruffは成功した。固定fixture切替の14件では、消費途中の切替拒否、PCM完全一致、旧音声と新音声の境界分離を確認した。実割り込み試験は上記のとおり不合格であり、相槌・take-turn各100件、stale提示、reconnect、dogfoodの受け入れは未完了である。


## 2026-09-07: 静音中のモデル状態と開始位相を分離する

実接続の取りこぼしを受け、同じ音声を「新規モデル」「前の発話の直後」「待機2秒／10秒」の状態から送り、開始位相を16ms刻みで変える診断を追加した。音声の正解境界を使う直前resetは原因切り分けだけに使い、製品では入力PCMの静音を条件とする。[状態・位相と全cohortの匿名比較](artifacts/vad-idle-state-2026-09-07.json)にはモデル・検出器・診断コード・fixtureのhashと各分母を保存した。

連続マイクではRMS 0.001未満の静音が700ms続いた後、256ms以上の間隔でMicVADの公開pause/startを呼び、モデル状態をresetする。受信済みframeをコピーしてreset後に処理し、直列化する。文中の600ms休止ではresetしない。待機frameは最大16件とし、処理遅延で上限を超えた場合はエラーを通知して処理を閉じる。終了中のresetからマイクを再開しない。非連続録音経路には適用しない。

発話確定には従来の強い確率300msの条件を残し、短い音声ではPCM活動を伴う確率0.2以上の積分値140msも根拠にする。ただし直近のPCM活動かつ確率0.4以上が192ms続くことを必須とする。閾値変更だけで実接続の取りこぼしを解消したとは扱わない。

| 診断 | legacy | v5候補 |
|---|---:|---:|
| 最初のtake-turn 4素材・位相／待機72条件、resetなしの確定数 | 44/72 | 0/72 |
| 同条件・静音中resetありの確定数 | 69/72 | 72/72 |
| 前の発話＋待機2秒後の全take-turn未検出 | 0/100 | 8/100 |
| 同じ連続入力条件の相槌未検出 | 14/100 | 44/100 |
| 同じ連続入力条件の文中休止の分割 | 0/100 | 16/100 |
| 静音resetありの非発声音の誤確定 | 9/120 | 6/120 |

この全cohort診断はPCMから仮想clockを進める実Silero検証であり、実Browserの100回受け入れではない。両モデルで300件すべての先行発話が1回確定し、対象発話の確定が先行発話から開始時刻を引き継いでいないことも確認した。v5は少数の位相比較では良好だが全cohortで回帰があるため、製品モデルはlegacyを継続する。非発声音の比較用FrameProcessorにもresetが作用するため、その16/120を以前のresetなしbaselineと同一条件の値とはしない。

[実割り込みの匿名結果](artifacts/livekit-interruption-idle-reset-2026-09-07.json)では、静音resetのみと確率積分追加後を独立4件ずつ実行し、いずれも3/4成功・1/4未判定だった。失敗する素材が入れ替わっており、改善を確実な再現性として主張しない。積分追加後の成功3件はlocal stop p95 773.3ms、decision後cancel p95約5.98ms、発話開始からcancel確認p95 796ms。各latencyの分母4・欠測1を保持した。両runの明示終了と所有Frontend／Backendコンテナの削除を確認した。

変更後の[通常応答pilot](artifacts/livekit-pilot-2026-09-07-idle-weighted.json)は準備1回＋独立3回成功。transcript・全PCM出力・明示終了を照合し、gap・underrun・追加操作は0、TTFA p95約1,833.57msだった。これはthink:false・RAGなしの少数診断値で、通常応答の正式100回や人格・記憶の品質証明には代用しない。所有Frontend／Backendコンテナの削除も確認した。

Frontend単体461件・結合97件、型検査、buildが成功した。最初の全体テストで9件失敗した原因はAppのVADモックに今回使用する公開APIがなかったことで、モックを更新して全件を再実行した。buildには既存のbundleサイズ警告が残る。実割り込みの取りこぼし、相槌の検出・分類、全stack各100件の集計とその他の受け入れ条件は未完了である。


## 2026-09-07: take-turn 100件で生成完了後のキャンセル欠測を確認する

`1df4bba`の実装を固定し、ラベル付きtake-turn全100件を独立sessionで実行した。Playwrightは**80件成功・20件失敗**で不合格。明示終了100/100、所有Frontend・Backendコンテナ削除を確認した。共有推論サービスは停止していない。失敗runを保持し、[全分母と相関を再検証した匿名結果](artifacts/livekit-take-turn-100-2026-09-07.json)を保存した。

失敗20件は、VAD未確定3件、take-turn確定とlocal stop後にキャンセル確認がない16件、fixture投入時計の不確かさが20ms上限を超えた1件だった。最後の1件はsourceStartの幅が約21.3msで、VAD自体は確定・終了していた。失敗段階だけでVAD未検出4件と数えるのは誤りである。

集計器は全100件の存在、ラベルmanifestのhash・順序、独立session、旧応答と割り込み発話の相関、client/serverのclock・単位、Browser停止observerとtraceを照合する。別々の観測callbackで記録する停止・確認時刻には既存local stop照合と同じ2ms以内の差を許すが、ID不一致や異なるclockを補完しない。1件で確認時刻が1ms違っていた。量子化された別観測点の差として扱い、遅延の値にはtraceの元時刻を使う。

| 指標 | 分母 | 取得 | 欠測 | p95（取得分） |
|---|---:|---:|---:|---:|
| local playback stop | 100 | 96 | 4 | 3,254.25ms |
| turn decision | 100 | 96 | 4 | 3,253.25ms |
| decision後cancel | 100 | 80 | 20 | 約6.95ms |
| 発話開始からcancel確認 | 100 | 80 | 20 | 1,366.1ms |

有効な音声投入を検証できた99件のうち、キャンセル確認まで成立したのは80件、見逃し・未確認は19件だった。投入未検証1件を別途残し、99件に分母を縮めて100件条件を満たしたことにはしない。local stopとturn decisionは取得分のp95も3,000msを超える。cancelのp95が小さくても欠測20件があるため合格にしない。各latencyは既存artifactのmetric schemaで検証し、匿名性検査、raw manifest・trace・集計コードのhashを保存した。通常応答の100試行artifactとの統合、相槌・再接続等の評価は未完了である。

16件の確認不足では、Coreの`server_cancelled`記録はある一方、Browserへ`response_cancelled`が届いていなかった。Coreは生成終了時にterminal状態へ移り、その後のLiveKit配送で残りPCMの送出完了を待つ。既にCOMPLETEDの応答へcancel要求が来てもterminal不変条件により状態を変更しないが、preview／final判定の両経路は戻り値を確認せずキャンセル成功を記録していた。配送待機中のCOMPLETED応答で、成功観測1件・キャンセル通知0件となる単体再現も確認した。

まず計測を修正し、cancel結果が実際にCANCELLEDの場合だけ`server_cancelled`を記録する。修正前は完了済み応答のpreview／final回帰2件が失敗し、修正後は実cancelの正のケースも含めCore・adapter・contract・集計器113件が成功した。Coreの型検査とRuffも成功した。完了済み応答の再生停止・確認自体を直したわけではなく、生成終了と配送／再生終了の境界の修正、および修正後の実接続再測定が必要である。


## 2026-09-07: 全音声の出力確認まで応答をキャンセル可能にする

生成完了と再生完了の境界を修正した。CoreはLLM/TTS生成後、`ResponseCompletionPort`で出力完了を待ってからCOMPLETEDへ移る。LiveKitではPCM送出と総sample数通知を先に行い、Browserの全sample実出力確認を待つ。待機中は既存のcancel処理で中断でき、terminal状態を後から書き換えない。総sample数通知までの送出処理後、確認が10秒以内に届かなければ既存のFAILED経路へ進む。

`playback_completed.response_finished`を追加し、応答ID・連続した音声metadataの総sample数・最終sequenceが一致する全出力確認だけを受け付ける。途中prefixと同じsequenceでも最後の確認は1回送信できる。過去の`playback_stopped`は対象responseだけを停止する。元PCMと途中prefixのcodec offset検証は別途残る。

変更後の[通常応答pilot](artifacts/livekit-pilot-2026-09-07-output-completion.json)は準備1回＋独立3回成功。transcript・全PCM出力・明示終了を照合し、gap・underrun・追加操作は0件、TTFA p95は約1,846.06msだった。think:false・RAGなしの少数診断であり、人格・記憶や正式100回の合格へ広げない。

[実再生中のtake-turn先頭20件](artifacts/livekit-take-turn-20-2026-09-07-output-completion.json)は18成功・2失敗で、Playwright全件成功条件は不合格だった。音声投入・独立session・明示終了は20/20確認できた。失敗index 0・3はcandidateからmisfireとなり、割り込みのturn decisionは出なかった。対象音声付近のSilero最大確率は約0.420・0.443で、発話確定条件を満たしていない。判定が成立した18件はすべてBrowserのキャンセル通知と停止observer、旧responseと割り込みutteranceのtraceを照合できた。前の100件でキャンセル確認が欠けたindex 15・16・19は今回成功したが、残る80素材と位相差の再検証は必要である。

| 指標 | 分母 | 取得 | 欠測 | p95（取得分） |
|---|---:|---:|---:|---:|
| local playback stop | 20 | 18 | 2 | 1,974.2ms |
| turn decision | 20 | 18 | 2 | 1,974.2ms |
| decision後cancel | 20 | 18 | 2 | 約5.97ms |
| 発話開始からcancel確認 | 20 | 18 | 2 | 2,006.2ms |

見逃しは2/20で10%。欠測2件を各遅延の分母に残し、100件未満でもあるため受け入れは未達とする。匿名性・metric schema・全fixtureのhashと順序は集計器で検証した。両runの所有Frontend・Backendコンテナ削除を確認し、共有推論サービスは停止していない。

Backend単体・結合は3,920件成功・1件skip、Frontend単体463件・結合97件、Backend／環境の型検査231ファイル、Frontend型検査とbuildが成功した。出力待機中の正常完了・cancel・timeout、別responseやsequenceの確認拒否、重複確認、古い停止の対象を検証した。buildの既存bundleサイズ警告とBackendの既存非推奨警告は残る。VAD未確定、遅いpreview、全stackの相槌／take-turn各100件、再接続・stale提示・通常100件再測定・元設定の品質と相対境界の受け入れは引き続き未完了である。


## 2026-09-07: 先行認識の800msから発話前の静音を除く

先行認識の開始条件がcapture全体の800msだったため、最大2秒のpre-rollに無音が含まれていると、実音声200msでも一度きりのpreviewを消費していた。静音2秒＋実音声200msでpreviewが実行されるテストと、全無音でも実行されるテストは修正前に失敗した。pre-rollなしの対照は成功していた。

`SttSignalSpan`は先頭trimと同じ絶対値16超の最初のPCM16 sampleを起点に、以後の長さを追記ごとに数える。最初の閾値超過から800msを受信した場合にだけpreviewを実行する。文中休止もこの長さに含み、音声の削除・分割には使わない。低い静音閾値は発話と雑音の分類器ではなく、環境雑音が続く条件では発話開始の保証にならない。全無音はpreviewを行わず、発話終了時の通常認識へ渡す。終了済みcapture、容量超過、入力受付終了ではpreviewを追加しない。

関連Backend93件、型検査231ファイル、Ruffが成功した。語頭、負の閾値超過、静音境界、発話中休止、別captureへの持越し、1回だけのpreview、終了入力を検証した。FrontendとVAD確定条件は変更していない。

[実接続take-turn先頭20件](artifacts/livekit-take-turn-20-2026-09-07-preview-onset.json)は20/20成功。全件で音声投入、独立session、停止とキャンセルの相関、明示終了を検証し、所有Frontend・Backendコンテナ削除を確認した。各割り込み指標は取得20・欠測0だった。

| 指標 | 分母 | p95 |
|---|---:|---:|
| local playback stop | 20 | 1,110.4ms |
| turn decision | 20 | 1,110.4ms |
| decision後cancel | 20 | 約5.65ms |
| 発話開始からcancel確認 | 20 | 1,131.75ms |

前の20件ではpreviewがtake-turn 15件・indeterminate 3件・VAD未確定2件だった。今回は20件すべてpreviewがtake-turnとなった。実際のpreview入力時点でcapture全体は34,720〜42,240 samples、最初の静音閾値超過以降は12,806〜12,955 samples（約800.4〜809.7ms）であり、新しい数値traceで区別できる。本文・波形は匿名artifactへ含めない。

この20件はthink:false・RAGなしの診断で、最低100件の見逃し率評価は不合格のまま保存した。VADの確定条件は変えておらず、今回の未検出0件を位相依存の取りこぼし解消とはみなさない。全100素材・相槌への影響・その他の受け入れ条件は別途検証が必要である。


## 2026-09-07: 全100素材で相槌から始まる発話の判断遅れを確認する

`932a885`を固定した[実接続take-turn全100件](artifacts/livekit-take-turn-100-2026-09-07-preview-onset.json)は72成功・28失敗で不合格だった。VAD未確定1件、sourceStartの時計幅が約24.1msで20ms上限を超えた1件、先行判定backchannelの後に最終take-turnへ変わるが旧応答の実出力が既に完了していた26件である。26件すべてでBrowserの旧応答完了時刻が最終take-turn受信より早いことを照合した。

| 指標 | 分母 | 取得 | 欠測 | p95（取得分） |
|---|---:|---:|---:|---:|
| local playback stop | 100 | 98 | 2 | 3,684.95ms |
| turn decision | 100 | 98 | 2 | 3,684.1ms |
| decision後cancel | 100 | 72 | 28 | 約6.26ms |
| 発話開始からcancel確認 | 100 | 72 | 28 | 1,196.55ms |

投入を検証できた99件のうち27件はキャンセルまで成立しなかった。投入未検証1件を除いて分母を縮めることはせず、全100件条件は未達とした。明示終了100/100、所有Frontend・Backend削除を確認し、raw runを保存した。短い発話中心の先頭20件で全成功していても、長い素材を含む品質は証明できなかった。

先行認識を一度だけ行う制限を見直し、backchannel／indeterminateの後は追加800msを受信するごとに最大3回まで再判定する修正を追加する。take-turnを選んだ後、capture終了、入力受付終了、容量超過、preview taskのcancel後には再開しない。処理中のSTTと並行させず、待機中の発話全体STTを優先する。繰り返しの入力長と開始／終了はattempt別の数値traceへ記録する。音声を削ったり、相槌の分類語彙を変更したりはしない。

修正前は追加音声による再判定等の4テストが失敗、修正候補では5件成功した。さらに処理cancel時に再実行を作ってしまう問題を1件で再現して修正し、6件成功を確認した。本体へ適用後の関連Backend131件、Backend／環境の型検査231ファイル、Frontend型検査、Ruffが成功した。実接続による再判定の効果と相槌への影響は別途測定する。


## 2026-09-07: 相槌の誤cancelを全出力証拠と照合する

割り込み前に旧応答の初回packetとtrackの観測を固定し、終了時の全出力観測と同じresponseで結べるようにした。`report_backchannel.py`は既存のpacket／全出力検証器を使い、初回出力時計・source sample総数・RTP連続性・対象音声終了までの全出力を検証する。local stopとserver cancelの片方だけでも誤cancelとして数え、出力を確認できない試行は欠測とする。全cohort、独立session、投入境界、終了、匿名性、metric schemaと検証器hashも確認する。

[再判定修正後の実相槌4件](artifacts/livekit-backchannel-04-2026-09-07-bounded-preview.json)は、Playwrightの分類完了条件では2成功・2失敗だった。失敗2件はcandidateからmisfireとなり、分類へ進まなかった。4件すべてで旧応答が全sampleを出力したことをsource／packet／出力時計から照合でき、local stop・server cancelは0件だった。したがって誤cancel指標は取得4・欠測0・誤cancel0だが、最低100件を満たさず不合格のままとする。相槌のVAD未検出2件をこの指標の成功で隠さず、分類済み2/4と別に報告する。全4件の明示終了と所有Frontend・Backendコンテナ削除を確認した。

集計器の単体22件では、誤停止単独・誤cancel単独、2%境界、入力不足、時計・source・response不一致、未完了音声、欠測の分母維持、匿名性を検証した。全体STTの優先と入力終了後の再判定防止を加え、関連Backend133件が成功した。take-turn全100件と相槌100件での再判定の効果はまだ未検証である。


## 2026-09-07: 再判定後の全100件と、fixture開始時計の改善

`10927b6`を固定した[実接続take-turn全100件](artifacts/livekit-take-turn-100-2026-09-07-bounded-preview.json)は98成功・2失敗だった。失敗はindex 1のVAD未確定と、index 75のsourceStart幅約24msによる投入時刻の未検証である。後者はBrowserのlocal stop・cancel通知・最終take-turnまで到達していたが、時計上限20msを満たさず品質集計では欠測とした。全100件の明示終了、所有Frontend・Backendコンテナ削除を確認した。

| 指標 | 分母 | 取得 | 欠測 | p95（取得分） |
|---|---:|---:|---:|---:|
| local playback stop | 100 | 98 | 2 | 1,927.3ms |
| turn decision | 100 | 98 | 2 | 1,927.15ms |
| decision後cancel | 100 | 98 | 2 | 約6.49ms |
| 発話開始からcancel確認 | 100 | 98 | 2 | 1,951.15ms |

長い発話で先行backchannelの後に追加音声からtake-turnを選び、旧応答を停止・キャンセルできるケースを確認した。投入を検証できた99件に対して未成立1件、投入未検証1件を別途保持する。全100件のcoverageが揃わず、見逃し率・遅延の最終受け入れは引き続き不合格である。

相槌等へのfixture切替では、大きなPCM配列と開始要求を同じメッセージで送っていた。開始時計に転送・準備の待機が入る可能性を切り分けるため、workletへPCMを先に準備し、準備確認後にデータを含まないreplay要求を送るようにした。準備中は音声を消費せず、新しい開始要求の時刻を因果下限とする。再生途中・準備重複を拒否し、準備待ちは5秒timeout、終了後には再開しない。20msの検証上限、元PCM、正解sample位置は変更していない。新しい2テストは修正前に失敗し、修正後は全16件とFrontend型検査が成功した。実接続100件で時計幅が改善したかは次の測定で確認する。

VADについては、4msのPCM活動窓で開始を探し、その32ms前からモデルの96ms frameを揃える[オフライン候補](artifacts/vad-onset-alignment-2026-09-07.json)を全300素材・非発声音120条件で比較した。baselineは相槌未検出14/100・take-turn 0/100・文中分割0/100・非発声音誤確定9/120で、既存診断と一致した。候補は相槌未検出30/100・take-turn 10/100・文中分割0/100・非発声音誤確定2/120となり、発話検出が悪化したため採用しなかった。先行発話は両条件とも300/300で1回確定した。これはモデルframe区切りの候補診断であり、Browserの実入力や製品の変更ではない。再現scriptは`frontend/scripts/measure-vad-onset-alignment.mjs`、入力引数はfrontendディレクトリと新規出力パスである。


## 2026-09-07: 相槌100件の誤cancelと公式VAD候補

`b19cf06`固定の[実接続相槌100件](artifacts/livekit-backchannel-100-2026-09-07-prepared-fixture.json)はPlaywrightで77成功・23失敗だった。12件はVAD candidateからmisfireとなり分類されず、11件はtake-turnと判定され、local stopと旧応答のcancelを両方観測した。誤cancel指標は全100件を検証でき、89件継続・11件誤cancel・欠測0、誤cancel率11%で目標2%以下に未達である。未検出12件では旧応答の全出力を直接確認できたため、誤cancelなしに数えるがVAD品質の成功とはみなさない。

音声投入時計は全100件で20ms上限を満たした。最大幅はsourceStart 3.9ms、speechStart 4.3ms、speechEnd 6.4msだった。前回の大きな配列転送と開始指示を分けた変更により、今回の開始時計の未検証は0件になった。明示session終了100/100と、環境reportのteardown完了、所有Frontend／BackendのDocker削除を確認した。raw runは`prepared-fixture-backchannel-100-01`として保持する。測定条件は引き続きthink:false・RAGなしで、通常応答・人格・記憶品質の達成を示すものではない。

VADの声質依存を切り分けるため、[公式Silero v6.2.1の固定commit](https://github.com/snakers4/silero-vad/tree/7e30209a3e901f9842f81b225f3e93d8199902b1)からONNX、MITライセンス、参照wrapperを取得した。16kHz入力の契約は新規512サンプルと直前64サンプルで、reset時はRNN stateと文脈の両方を消去する。診断wrapperはこの契約を実装し、62フレームについてPython ONNXの参照計算とWASMの確率差最大約0.00000104（許容0.00001）を確認した。文脈継続・全reset・不正frame拒否の単体確認も成功した。

[全素材の候補比較](artifacts/vad-official-context-2026-09-07.json)は以下のとおり。相槌・take-turn・文中休止は各100件、非発声音は120件で、モデル前に同じ先行発話と待機を入れるオフライン診断である。

| 候補 | 相槌未検出 | take-turn未検出 | 文中分割 | 非発声音誤確定 |
|---|---:|---:|---:|---:|
| 現行legacy・比較元 | 14/100 | 0/100 | 0/100 | 9/120 |
| v6.2.1・現行detector | 27/100 | 0/100 | 0/100 | 0/120 |
| v6.2.1・高確率の確認時間を短縮 | 26/100 | 0/100 | 0/100 | 0/120 |
| v6.2.1・静音resetなし | 38/100 | 0/100 | 0/100 | 未測定 |
| bundled v5・直前64サンプル追加 | 27/100 | 0/100 | 2/100 | 未測定 |

短縮候補は強い確率を0.5・128ms、補助の高確率を0.7・96msとし、弱い確率を単純に許す変更では改善を主張しない。v6.2.1とlegacyのどちらかで検出できればよいという合算でも、legacy未検出14件のうち回復は1件だけだった。新モデル単独への置換、確認時間変更、静音reset撤去はいずれも採用しない。製品のVAD・分類器は変更していない。候補の入力・モデル・wrapper・script・rawのhashはartifactに保存し、作業用scriptとrawは`/tmp/issue150-silero-v621/`へ保持する。


## 2026-09-07: 曖昧な短反応で即座に旧応答をキャンセルしない

相槌100件の誤cancel11件では、STTが短い非語彙的な反応を返し、分類器が登録外の非空文字列をすべてtake-turnにしていた。照合時だけUnicode幅と片仮名を正規化し、完全な既知相槌を同じ判定へ揃える。認識本文・履歴本文を書き換える処理ではない。

促音・長音を伴う一音の反応と単独の鼻音は、相槌とも停止指示とも断定せず`indeterminate`とする。ただし質問符がある場合や、後続に停止・訂正・質問等の語が続く場合はtake-turnへ進む。完全一致の既知相槌の扱いは維持する。この判定を発話冒頭で返しても追加previewと発話全体のSTTを妨げず、最終判定も保留なら既存の非take-turn経路で旧応答を続け、次のpromptに持ち越さない。誤認識を正しい相槌本文へ置換して評価する修正ではない。

追加テストは修正前17失敗・23成功、修正後40成功だった。最終判定で保留しても旧応答と記憶境界を維持する2件を加え、分類・Core・追加preview・相槌集計の関連72件、Ruff、分類器の型検査が成功した。質問符付きの短反応、後続指示、片仮名・半角の既知相槌、保留から最終take-turnへの遷移を検証した。実接続100件への効果とtake-turn見逃しへの影響は改めて測定する。

VADの音量依存も[全300素材と非発声音120件](artifacts/vad-model-gain-2026-09-07.json)で比較した。モデル入力だけを4倍、絶対値1でclipし、detector・録音・STTへ渡す元PCMは維持する診断である。legacyは相槌未検出14件から11件へ減ったが、非発声音誤確定9件から26件へ増えた。v6.2.1は相槌未検出27件から25件、文中分割0件から1件、非発声音誤確定0件だった。両方ともtake-turn未検出0件で、音量補正だけでは全体品質を満たさず採用しない。製品VADは変更していない。


判定修正後のBackend全単体・結合テストは3,992件成功・1件skip・4件warningだった。Frontend型検査も成功した。測定harnessはラベル不一致を検査する前に旧応答の全出力完了またはcancelを観測し、保留判定で観測を打ち切らないようにした。厳密なラベル不一致は引き続き試行失敗として残す。相槌集計にはbackchannel／take-turn／indeterminate／検証できない最終判定の件数を分離して追加し、保留を相槌の分類成功に置き換えない。停止がなくても全出力証拠がなければ欠測とする検証は維持する。


## 2026-09-07: 判定保留後の相槌100件は誤cancel 0件

`4b083f7`固定の[実接続相槌100件](artifacts/livekit-backchannel-100-2026-09-07-ambiguous-reaction.json)で、全100件の投入時計・旧応答の全出力継続・独立session・終了を検証した。local stopとserver cancelは各0件、誤cancel指標は取得100・欠測0・誤cancel0件で、最低100件と2%以下の条件を満たした。前回の誤cancel11件を欠測や除外へ移した結果ではない。

分類はbackchannel79件、indeterminate11件、最終判定なし10件だった。保留11件は旧応答のsource／packet／出力時計による全出力継続を確認でき、VAD未確定10件も別に残した。Playwrightの厳密なラベル一致は79成功・21失敗で、分類成功やVAD品質の全達成を示すものではない。試行のラベル不一致を調べる前に旧出力の終端まで観測することで、保留時も継続を直接検証した。

全100sessionを明示終了し、環境reportのteardown完了と所有Frontend／Backendコンテナの削除を確認した。測定には約14.7分を要し、raw runは`ambiguous-reaction-backchannel-100-01`として保持する。相槌誤cancelの条件が達成されても、VAD、take-turnへの影響、最新通常応答100件、人格・記憶品質、再接続等の残る条件は別途検証する。


## 2026-09-07: 短い未確定音声だけを補うVAD候補

[WebRTC由来のlibfvadブラウザ実装](https://github.com/OzymandiasTheGreat/libfvad-wasm/tree/2692578b7f2af573e3a6efb84e4356b41250d414)を固定commitから取得し、全4モードを同じ300音声・120非発声音で比較した。mode 0単独は音声を全件検出したが非発声音誤確定41件、mode 3単独は相槌未検出5件・take-turn途中分割4件・非発声音誤確定9件だった。Sileroの低い確率とWebRTCの判定をそのまま合成した候補も、相槌未検出5件・非発声音誤確定36件となり採用しない。

代わりに、Sileroの確率と元PCMを維持し、PCMの静音で終了するまで未確定だった短い候補だけを補う方式を[オフライン評価](artifacts/vad-short-utterance-fallback-2026-09-07.json)した。PCM活動区間が700ms以下で、WebRTC mode 0が音声とした割合60%以上のframeを補助根拠にする。1024点Hann窓のスペクトルで、2つの狭い帯域への集中度が0.9未満、平坦度が0.3未満のframeだけを積算し、補助根拠が160ms以上なら元の開始時刻を維持して確定・終了する。通常のSilero確定や600msの文中休止規則は維持する。確定が遅れても音声の冒頭を捨てないため、製品へ組み込む際はCoreのpre-rollと開始・終了の通知順序を実接続で検証する必要がある。

最初の単一帯域条件では既存300音声の未検出を0にできた一方、短い音・雑音を追加した216条件で新しい誤確定18件があった。2帯域の集中度と平坦度を分ける候補でも集中度0.98では6件が残ったため、これらは採用しない。上記0.9の候補では、全300音声の未検出・冒頭欠落・早期終了・分割がすべて0件、既存非発声音の誤確定は現行と同じ9/120件だった。追加216条件の誤確定は現行70件・候補70件、新規誤確定0件だった。既存の誤検出を0へ書き換えず、比較元との差として評価する。

全300素材を開始位置0/16/32/48/64/80msで繰り返した1,800条件の比較では、相槌未検出は現行85/600件・候補0/600件。take-turnは両方とも未検出2/600件・冒頭欠落6/600件、早期終了と分割0件だった。文中休止600件は両方とも全検出・欠落・早期終了・分割0件。先行発話は両方とも1,800/1,800件で1回だけ確定した。候補の確定遅延p95は各cohort約758〜760msだった。Browser実接続の受け入れとは区別し、候補の製品実装と実接続検証へ進む。

固定ライブラリの埋め込みWASMは15,331 bytes、importなし。ライセンス本文と元repository、モデル・補助処理・検証script・rawのhashを記録した。比較scriptとrawは`/tmp/issue150-libfvad/`、`/tmp/issue150-vad-fusion/`、`/tmp/issue150-short-fallback/`、`/tmp/issue150-short-fallback-v2/`へ保持する。この段階では製品VADを変更していない。


## 2026-09-07: 短音声の補助判定を製品の録音処理へ組み込み

候補で検証した固定WASMと自作の型付きwrapperを採用し、継続音声の録音処理へ接続した。主VADの確率と元PCMは維持する。10ms単位の補助判定で残る端数を保持し、主VADの静音resetでは補助状態も再生成する。停止時は処理待ちのframeを終了してからWASMを解放する。短い候補を静音終了時に確定する場合も、元の開始時刻の通知、終了通知の順序を維持する。

[製品実装によるオフライン再検証](artifacts/vad-short-speech-production-2026-09-07.json)では、音声300件・既存背景音120件・追加の短い電子音と雑音216条件が候補版と全行一致した。音声300件の未検出・先頭欠落・早期終了・分割は0件、確定遅延p95は各cohort約757.7ms。既存背景音の誤確定9/120件、追加条件の誤確定70/216件は比較元から増えていない。背景音集計の`baseline_false_starts`16件はライブラリ既定FrameProcessorであり、製品の比較元9件とは区別する。

関連テスト64件は開始・終了順序、状態resetの同期、初期化失敗時の解放、同梱WASMの実行、PCM不変、異常入力を検証した。Frontend全単体490件・モジュール97件、型検査・ビルドが成功した。全単体の初回ではAppの補助VAD依存mock未追加により9件が失敗し、mockを追加した再実行で成功した。ビルドには既存の大きなchunkに関する警告が残る。実接続での開始・終了通知とpre-roll、分類・cancelの検証はこのオフライン結果に含めない。


`c907f06`固定の[実接続少数試験](artifacts/livekit-short-speech-pilot-2026-09-07.json)では、相槌4/4件、take-turn4/4件が成功した。相槌のうち3件は短音声補助により開始・終了を同じcallbackで確定し、開始時刻を約1,246ms前の候補先頭に保ったままCoreへ通知した。全4件で最終相槌判定と旧応答の全出力継続、停止・cancel0件を検証した。take-turn4件も投入時計・旧応答相関・cancelを全件検証し、local stop／turn decision p95は1,106.1ms、decision後cancelは6.934ms、全cancelは1,130.35msだった。

両試験は追加のthinking無効・RAGなし条件であり、通常設定での人格・記憶品質の証拠にはしない。全8sessionの明示終了、両runのteardown完了、所有Frontend／Backendコンテナの削除を確認した。最低100件を満たさないため、相槌・take-turnの最終合否はまだ未達として記録する。raw runは`product-short-backchannel-pilot-01`と`product-short-take-turn-pilot-01`に保持する。

## 2026-09-07: 補助VAD組み込み後のtake-turn 100件と再生前の時計管理

`c907f06`固定の[実接続take-turn 100件](artifacts/livekit-take-turn-100-2026-09-07-short-speech.json)は98成功・2失敗だった。投入時計を検証できた99件のうちcancel確認98件、見逃し1/99件で1%を超え、未投入1件も残るため受け入れは未達とする。local stop／turn decision／decision後cancel／全cancelの有効98件のp95は各1,920.90／1,920.75／5.955／1,948.15ms。各指標の欠測2件を残し、有効値だけで最終合格にしない。

0始まりindex 6は約844msの発話がVAD候補のままmisfireとなり、主VADの最大確率は約0.273だった。700ms以下を対象とする短い候補の補助条件からも外れた。この素材は事前の6位相比較では全て検出できており、実入力との差を引き続き調べる。index 23は割り込み投入前の初回応答で`renderer/render_clock_unreconciled`が発生した。音声受信・decode・出力開始は未観測で、Coreの発話確定と応答開始だけが記録された。

後者では測定harnessが再生成功後にsession IDを保存していたため、初期応答失敗時の終了確認も欠測となった。今回の99件の終了確認を100件へ補完しない。全run終了後のteardown完了と所有Frontend／Backendコンテナの削除は別に確認した。raw runは`product-short-take-turn-100-01`へ保持する。測定所要時間は約15.6分。

再生前の時計不一致を固定clock列で再現する単体テストは修正前1失敗・26成功だった。PCMが未到着で提示済み・未照合の音声区間がない場合だけ、不要な時計履歴を破棄して再取得する修正後は27件成功、型検査・ビルドも成功した。PCM受信後は従来どおり不一致をエラーにし、出力済みの未照合区間を時刻補正で確認済みへ昇格しない。測定harnessはsession作成応答からIDのみを先に記録し、初回応答との一致を検証するようにした。これらの修正後の実接続再検証は別のrunとして実施する。

`41ce9c4`の[修正後take-turn 8件](artifacts/livekit-take-turn-pilot-2026-09-07-idle-clock.json)は6成功・2失敗だった。再生・transportエラーは0件、全8件の投入時計・独立session・終了応答を検証でき、teardown完了と所有Frontend／Backendコンテナ削除も確認した。初回再生の失敗を含む100件の証明ではない。

残るindex 1と7はいずれも約844msの発話で、主VADの最大確率は約0.480／0.422あったが確定条件を満たさずmisfireになった。700ms以下の短音声補助の対象外でもある。前の100件で失敗したindex 6は今回成功したため、素材を除外せず実入力の位相差も含めて改善を検討する。次は補助対象の長さを変える候補を音声・背景音・短雑音の固定条件で比較し、誤確定を増やさないことを確認してから採否を決める。


## 2026-09-07: 弱い発話を補う活動区間の上限を1秒へ拡張

実接続で残った約844msの発話を含めるため、補助対象の活動区間を700msから1,000msへ変更した。音声割合・スペクトル条件・積算根拠160ms・静音終了時だけの補助は維持する。1秒を超える候補には補助を使わない。主VADの通常確定条件も維持する。

[候補と製品のオフライン比較](artifacts/vad-one-second-fallback-2026-09-07.json)では、固定音声300件の未検出・先頭欠落・早期終了・分割0、背景音の誤確定9/120件、既存短雑音の誤確定70/216件を維持した。追加の800/950/1100msの電子音・二音・雑音162条件も、比較元41件・候補41件で新規誤確定0だった。全300素材×6開始位置では、take-turn未検出が2/600件から0/600件、先頭欠落が6/600件から4/600件へ減った。相槌・文中休止は各600件の未検出・先頭欠落・早期終了・分割0。各cohortの確定遅延p95は約758〜760msだった。

製品の再現scriptへ`phase-sweep`オプションを追加し、全1,800行が候補と一致することを確認した。再実行はFrontendから`node scripts/measure-vad-runtime-cohorts.mjs NEW_OUTPUT.json legacy src/lib/audio/utterance-detector.ts phase-sweep`で行う。追加の768/960/1000ms境界テストは修正前3失敗・既存13成功、修正後はFrontend全単体494件成功、型検査・build成功。実接続での改善は別runで確認し、この診断だけで100件受け入れの達成とはしない。

`cb42471`の[1秒補助採用後の実接続8件](artifacts/livekit-take-turn-pilot-2026-09-07-one-second.json)は8/8件成功した。前回失敗したindex 1・7も含め、全8件で投入時計・take-turn判定・旧応答cancel・独立session・明示終了を確認した。local stop／turn decision／decision後cancel／全cancelのp95は1,738.30／1,737.65／12.427／1,768.15msで各8件取得・欠測0。transport・再生エラーは0件、teardown完了と所有Frontend／Backendコンテナ削除も確認した。少数試験のため最低100件の条件はまだ未達とし、同じ実装を全100件で再測定する。raw runは`one-second-take-turn-pilot-01`へ保持する。


## 2026-09-07: 1秒補助後のtake-turn 100件が全成功

`81b36ce`固定（runtimeは`cb42471`と同一）の[実接続take-turn 100件](artifacts/livekit-take-turn-100-2026-09-07-one-second.json)は100/100件成功した。全100件の投入時計・独立session・take-turn判定・旧応答cancel・明示終了を検証し、見逃し0件・投入未検証0件だった。local stop／turn decision／decision後cancel／全cancelは各100件取得・欠測0、p95は1,931.45／1,930.50／6.951／1,953.35msで、各絶対目標を満たした。

再生・transportエラー0件、投入時計の20ms上限超過0件。全runのteardown完了と所有Frontend／Backendコンテナの削除を確認した。raw runは`one-second-take-turn-100-01`として保持し、測定は約14分だった。凍結WebSocket baselineの4つの割り込み指標は`not_applicable`であり、この4指標の相対比較対象はない。他の比較可能な指標の相対評価は通常応答で行う。

集計は従来のmetric schemaと匿名性検査に加え、cohort出力全体のschemaと整合性検査を通した。件数・欠測理由・指標名・計測境界・ステータス・合否の不整合を検出する関連68件のテストとRuffが成功した。欠測がある過去の結果や分類保留のある相槌結果も正しく検証し、合否や数値を書き換えないことを確認した。最終schema適用前の暫定集計は作業用ファイルへ保持し、最終検証後も今回の全件数・指標・合否が変わっていないことを照合した。

この結果はthink:false・RAGなし条件でのtake-turn見逃し率と4つの割り込み遅延の達成であり、最新相槌100件、通常応答100件、VAD取込境界、人格・記憶品質、再接続、stale／gap等の残る受け入れを含まない。次に同じ音声実装で相槌100件を再測定する。


## 2026-09-07: 1秒補助後の相槌100件の誤キャンセル率

`d6d4f22`固定の[実接続相槌100件](artifacts/livekit-backchannel-100-2026-09-07-one-second.json)は、誤キャンセル1/100件（1%）で2%以下の目標を満たした。全100件の独立session・投入時計・明示終了を確認し、音声継続の欠測は0件だった。99件は旧応答の全出力を確認し、1件はlocal stopとserver cancelを観測した。

分類結果は相槌80件・保留19件・take-turn誤判定1件。harnessは保留も失敗にするため80成功・20失敗、プロセス終了コードは1であり、全試行成功とは記録しない。0始まりindex 24のtake-turn判定が唯一の誤キャンセルだった。誤キャンセル指標のstatusは失敗観測を含む`failed`のまま、100件・欠測0・許容率2%の評価結果だけを合格とする。

匿名性、metric schema、cohort全体schema、件数・分母・欠測理由・合否の整合性検査を通過した。時計の20ms上限超過とtransportエラーは0件。teardown完了と所有Frontend／Backendコンテナの削除も確認し、raw runは`one-second-backchannel-100-01`へ保持する。実行中の音声runtimeと測定harnessは変更していない。

この結果はthink:false・RAGなし条件の相槌誤キャンセル率についての証拠である。分類保留を相槌成功へ補完せず、人格・記憶品質や他の未測定cohortの達成には流用しない。次に同じ音声実装で通常応答の準備5回＋独立100試行を行う。


## 2026-09-07: 1秒補助後の通常応答100件

`e506b2f`固定（音声runtimeは`cb42471`と同一）の[通常応答測定](artifacts/livekit-controlled-2026-09-07-one-second.json)は、準備5回と独立100試行が全成功した。全105件で明示終了を確認し、本測定100件の固定transcript一致・同一初期状態・独立ID・受信／decode／実出力時計・全PCM出力を集計時に検証した。raw runは`one-second-controlled-100-01`に保持する。

TTFAのp50は1,824.15ms、p95は1,900.385msで2,000ms以下を達成した。p50の改善目安1,000ms以下には達していない。utterance確定p95は307.518msで800ms以下、first text p95は382.218ms、LLM完了p95は549.682msだった。本測定100件の処理失敗・追加操作・underrun・gap回数・gap合計・最大gapは0。これをdogfoodや同一session3往復の証拠へ代用しない。

[凍結WebSocketとの自動比較](artifacts/livekit-controlled-2026-09-07-one-second-evaluation.json)は`latency_only`全体不合格を維持する。今回の通常cohortには割り込み4指標とVAD境界2指標がない。割り込み4指標は別のtake-turn100件で取得済みだが、この通常artifactへ値を補完していない。`response_decision`・`stt_start_latency`・`utterance_finalized`の相対判定も不合格であり、同名eventの実観測境界が異なる点の監査が残る。

匿名性とschema検証は成功した。Backend CPUは観測区間平均32.406%（1 coreを100%）、sample上のcontainer charged memory最大299,040,768 bytes。共有host GPUは利用率最大98%、使用量合計のsample最大7,748,976,640 bytesで、専有GPU測定ではない。Backend未起動の6sampleは欠測理由付きで保持した。本測定100件のbrowser音声RTP payloadは送信1,339,711 bytes・受信4,084,538 bytes、下り受信17,467 packets・loss 0・通信統計欠測0件。上りlossや全wire量の証拠ではない。

teardown完了と所有Frontend／Backendコンテナの削除を確認した。測定は引き続きthink:false・RAGなし条件であり、通常設定の人格・記憶応答品質の証明は未完了である。

## 2026-09-07: 再接続の欠測と率の丸めによる誤合格を防ぐ

再接続評価が「100試行中99件回復」に対し遅延値1件だけでも合格する問題を再現した。10秒以内に回復した全件と遅延値の件数を照合し、不正な件数・非有限値・負値・10秒超の成功値を拒否するよう修正した。全件タイムアウトはp95を0へ補わず`null`として不合格にする。再接続の最低試行数・99%・p95 3秒・重複0の条件は変更しない。

VAD、相槌・take-turn、dogfood gap・処理失敗率は、表示用basis pointsを丸めてから比較すると閾値をわずかに超えた率も合格になる。そのため合否は元の件数比で判定し、表示用の丸めは維持する。目標値は変更していない。

回帰テストは修正前22失敗・23成功、修正後は既存の音声指標・cohort検証を含む関連153件が成功した。Ruffと差分検査も成功。今回保存した通常100件の自動比較結果は、修正前後で完全に一致することを確認した。実際のネットワーク障害後のcontrol／audio回復100件は、この評価関数の検証だけでは達成扱いにしない。


## 2026-09-07: 会話を変えないcontrol往復の実測

再接続測定のcontrol確認に備え、private transportへ`control_probe`／`control_probe_ack`を追加した。状態同期要求は世代やCore状態を変えるため、周期的な疎通確認には流用しない。probeは現在のparticipant・世代・利用可能phaseでのみ応答し、Core通知・応答の中断・世代変更を行わない。BrowserはUUIDと世代で送受信を相関し、最大1件を500msだけ保持する。通常会話からの自動送信は追加していない。

送信完了だけの誤成功、異なるnonce／世代、遅着ack、切断後の古い送信失敗、時計逆転、500ms後のtimer遅延をテストした。期限の境界ケースは初回1失敗・既存を含む515成功から修正し、最終Frontend全単体516件・結合97件が成功した。Backendの認可・世代・lifecycle・private契約54件とrunner分離35件も成功。型検査、build、変更PythonのRuff、差分検査を完了した。

`d642475`固定の`control-probe-session-01`で、実Ollama／Whisper／VOICEVOX／LiveKitによる1応答の再生中に3probeを送信し、3/3往復に成功した。同一Browserの単調時計による往復時間は17.6／4.7／4.7msで、全probeを異なるUUID・同じ世代0へ相関した。旧応答の再生中であること、追加応答やcancelがないこと、transportエラー0を確認した。

さらにBackend traceの元sample数とBrowser packet・出力時計・全出力完了を照合し、168,960/168,960 samplesの出力、gap合計・最大gapとも0を検証した。session明示終了、teardown完了、所有Frontend／Backendコンテナ削除も確認した。raw manifestとtraceは当該run rootへ保持し、再実行コマンドは`scripts/voice_quality/README.md`に記載した。

これは障害なし・1 session・3probeの診断である。再接続の100独立試行、network回復時刻とBrowser時計の対応、実音声の復旧時刻、重複再生0の受け入れは未完了。次は専用bridgeの接続先とreadinessが一致する測定Profileを用意し、実際の障害注入へ結ぶ。


## 2026-09-07: 専用bridgeのProfileと実Browser／Core経路

`integration-voice-fault`を追加し、LiveKitのtoken接続先とreadinessを19880へ揃えた。Playwrightとorchestratorは同じ明示Profileを使用する。通常Profileへ19880を混在させず、専用Profileでは7880／17880を拒否する。共有推論サービスとtest用Frontend／Backendは維持し、run data rootとキーを分離する。起動前にはpurpose・test label、専用bridge、接続の排他性、19880/tcp・19881/tcp・19882/udpのloopback公開を検査する。

関連258テスト、通常・dogfood・共有推論を含むProfile回帰306テスト、Frontend結合101テストが成功した。これらのBackendテスト範囲には重複がある。型検査、変更PythonのRuff、差分検査も成功した。資源samplerは専用Profileでも当該test data rootの所有Backendだけを対象にする。

`dfe3e2c`の最初のBrowser診断`fault-control-probe-session-01`は音声開始前に失敗し、probe0件・明示終了未確認だった。Profile／readinessとBackendの専用キーは一致していたが、RTC接続が完了しなかった。teardownと所有Frontend／Backendコンテナ削除は別に確認した。失敗runは保持する。

同じ専用サービスへのSDK単独接続も20秒でtimeoutした。hostからbridge IPへ到達できることとUDP待受を確認したうえで、`node_ip: 127.0.0.1`とloopback候補の追加を外したところ、SDK接続は約548msで成立した。このSDK診断はroom削除後にプロセスが終了コード134で異常終了したため、終了処理まで成功した試験とは扱わない。診断の失敗段階とsession作成HTTP statusもharnessへ追加した。

`7579d8a`の`fault-control-probe-session-02`では、実Browser／Core／音声サービスによるsession作成がHTTP 200、再生中の3/3制御往復が17.6／4.3／4.4msで成功した。全probeは別UUID・同世代で、追加応答・cancel・transportエラーは0。Backend traceのsample数とBrowserの受信・decode・実出力時計を照合し、168,960/168,960 samplesの全出力、gap合計・最大gapとも0を検証した。専用Profileと19880のreadiness、Backend資源84sampleの取得も確認し、起動前の4sampleは未起動の理由付き欠測として残した。

アプリ側のrunは終了コード0で、session明示終了、teardown、所有Frontend／Backendコンテナ削除が確認できた。最後に専用LiveKitとbridgeも削除した。通常dev・dogfood・共有推論サービスへの設定変更や障害注入は行っていない。

これで専用bridge上の実Browser／Core制御・音声経路を確認できたが、今回のBrowser診断には切断を入れていない。再接続100件、network回復とBrowser時計の対応、controlと実音声の回復時刻、重複再生0の受け入れは未完了である。


## 2026-09-07: 障害runnerとBrowserの時計較正

`f1b49e2`で、Python障害runnerの標準入出力によるnonce付き時計応答と、Browserへの
`page.evaluate`をNodeの単調時計で囲む処理を追加した。診断前後にそれぞれ5sampleを取得し、
全sampleで共通のoffset区間が成立することと、合成した誤差幅20ms以下を要求する。
異なるepochを直接引かず、ns値は文字列として受け取る。復旧後の制御応答に対しても、
要求の送信が復旧時刻の上限を過ぎていなければ回復確認に使わず、遅延は復旧時刻の下限から算出する。
時計不一致・過大誤差・値の逆転・欠測・遅着ackを含むFrontend関連16件、
専用networkと標準入出力protocolのBackend28件、型検査・Ruff・差分検査が成功した。

実Browser／Core診断`fault-clock-session-01`は終了コード0。
前後の時計対応区間は共通に成立し、合成誤差幅は1.929114msだった。
音声再生中の3probeは全件成功し、往復時間は17.0／3.6／4.7ms。
既存のpacket出力検証器で受信・復号・出力時計を再照合し、Backendのsession・utterance・responseに
一致する元sample数／captured数／padding数と、全168,960 samplesの出力完了を検証した。
出力時間3,520ms、gap合計・最大gap・underrunはいずれも0。

session明示終了、時計probe子processの終了、teardown、所有Frontend／Backendの削除を確認した。
最後に専用LiveKitとnetworkも削除した。raw証跡は当該run rootへ保持している。
このrunは時計較正を追加した障害なし診断で、切断・復旧100試行には含めない。
実際の切断をBrowser診断へ組み込む処理、復旧後の新しい受信音声と実出力の相関、
重複再生の判定は引き続き未完了である。


## 2026-09-07: 初回の実Browser／Core切断診断

`b51bf12`で専用bridgeを2秒切断する`--network-fault`を追加した。
診断時だけ全packetの数値metadataと出力時計通過済み区間を保存し、
復旧後に受信した非無音packetの出力を追跡する。音声本文は記録しない。
復旧処理開始～TCP疎通成功を復旧時刻の範囲とし、時計較正の誤差とは分けて残す。
切断前の3control probeが成功した場合だけ障害を注入し、切断中の実timeoutも確認する。
Frontend関連74件、Backend関連65件、Frontend全単体534件、型検査・Ruffが成功した。
これらのFrontendのテスト範囲は重複している。

実測`network-fault-session-01`は終了コード1で、制御・音声の復旧条件を満たさなかった。
専用networkの切断／復旧／TCP疎通は全て成功し、切断中のcontrol timeoutを3件確認した。
前後の時計較正は成立し、合成誤差幅は1.816111ms。
復旧処理開始からlink復旧までは約111.46ms、TCP疎通までは約112.31msだった。
しかし復旧後10秒以内のcontrol ackも、新規受信packetの非無音出力も確認できなかった。

Browserは`RTP timeline discontinuity`を検出し、`rtp_timeline`段階で接続全体を切断していた。
その観測はTCP疎通成功時刻の上限から約3,854.58ms後。
`PacketOutputTracker`がRTP timestampと連続packet番号の不一致を拒否し、
roomの例外処理が`failTransport`を呼ぶ経路と一致する。
出力確認できたのは切断前の184区間・22,080 samplesまでである。
この範囲の重複区間は0だったが、再生経路が途中で失敗したため全体の重複再生0とは結論しない。
後続の判定では再生経路のエラー数と出力観測が揃っているかを明示し、
観測済み部分の重複0だけで合格しないようにした。

session明示終了、時計・障害runner子processの正常終了、teardown、
所有Frontend／Backendコンテナの削除を確認した。専用LiveKitとnetworkも最後に削除した。
失敗したmanifestとtraceは当該run rootへ保持し、成功runへの置換は行っていない。
次はRTP欠落時の再生中断と接続全体の終了を切り分け、Coreの応答中断・世代同期と
整合する復旧処理を検討する。再接続100件と3,000ms目標は未達のままである。


## 2026-09-07: RTP不連続と世代同期後のマイク再開

`011174e`で、RTP timestampの正の20ms単位の欠落をPCM出力前に検出し、
当該応答の停止位置通知・Coreへのcancel要求・状態同期を送る処理を追加した。
確認できていないsampleを全出力済みへ補完しない。停止済みの旧trackは再構築しない。
またBackendでは世代変更時に旧microphone readerが終了したままになる経路を修正した。
旧readerの終了を待って現世代のreaderを開始し、同世代の同期再送で重複させず、
購読解除後には再開しない。participantとsession所有権も照合する。

マイク再開のテストは修正前に1失敗・1成功、修正後は音声runtime／lifecycle／private契約の
関連108件が成功した。Frontend全単体544件、型検査・build・Ruffも成功した。
停止済みtrackを「未接続のgraphとして作る」とした旧テストは、graph自体を作らない期待へ更新した。
実切断診断には、回復測定の10秒窓の後に同じsessionの次発話を入れる検証を追加した。
これは回復時間の代替値として扱わない。

`network-fault-session-02`は`RTP packet sequence invalid`で失敗した。
`0f18c81`で数値contextを追加した`network-fault-session-03`では、受信packet番号は期待どおり
だったが、同じ送信元の前後のRTP timestampが完全に同じであることを確認した。
この観測だけで同一payloadの再送とは断定しない。

`10826bf`ではworker内の限定された保持領域で同じ送信元・RTP timestampのprimary payloadを
比較し、完全一致の場合だけ独立Opus復号前に除外する処理を追加した。
内容が異なる場合は`rtp_packet_payload_conflict`として失敗させる。
Frontend全単体547件、続く実worker経路・filter関連29件、型検査・buildが成功した。
workerテストではnative配送を保ちながら、重複を二度復号せず受入packet番号も連続することを確認した。
しかし`network-fault-session-04`はdecoder段階で失敗した。

`438a285`で任意例外本文を含まない固定decoder理由を保存し、関連29件と型検査を確認した。
続く`network-fault-session-05`では`rtp_packet_payload_conflict`を実際に取得した。
同じ送信元・RTP timestampに異なるprimary payloadが届いている。
同一payloadの重複として除外した件数は0で、RTP timestampだけによるpacket同一性の仮定は
さらに検証が必要である。[Encoded Transformの仕様](https://www.w3.org/TR/webrtc-encoded-transform/#rtcencodedaudioframemetadata)
には受信音声のRTP sequenceNumberも定義されているため、次は実Browserでその取得と
packet識別・並びの照合を確認する。現在の取得コードはsequenceNumberを保存していない。

| run | 測定版 | 時計対応の幅 | 結果 |
|---|---|---:|---|
| network-fault-session-02 | 011174e | 1.665336ms | RTP並び不整合、復旧・次発話未達 |
| network-fault-session-03 | 0f18c81 | 1.697722ms | 同じ送信元・同じRTP timestampの再到着を確認、復旧未達 |
| network-fault-session-04 | 10826bf | 2.129354ms | decoderエラー、復旧・次発話未達 |
| network-fault-session-05 | 438a285 | 1.746585ms | 異なるprimary payloadの競合、復旧・次発話未達 |

4runとも実切断・network復旧・切断中のcontrol不通は確認できたが、復旧後10秒内の
control／audio成功は得られず、後続発話も失敗した。マイクreader再開は単体・結合経路での
検証までで、これらのrunから実会話継続に成功したとは結論しない。
全runでsession明示終了、子process終了、teardown、所有Frontend／Backend削除を確認した。
最後に専用LiveKitとnetworkも削除し、失敗rawは各run rootへ保持した。
再接続100試行、成功率99%、p95 3,000msの受け入れは未達のままである。

## 2026-09-07: RTP連番照合と再接続後の次発話

`38a8e64`で、復号失敗直前の最大8packetについて送信元・RTP timestamp・RTP連番・
primary payloadサイズだけを保持した。連番は符号付き表現も16bitへ正規化する。
音声本文、payload、hash、任意例外本文は保存しない。
`network-fault-session-06`では同じRTP timestampに連番1093・120bytesと
連番1094・80bytesが届いた。したがって、先行runの競合を同一packetの再送とは扱えない。

使用中の[LiveKit v1.9.7の公開実装](https://github.com/livekit/livekit/blob/v1.9.7/pkg/sfu/downtrack.go)
ではmute／close時にOpusの無音frameを追加し、
[RTP munger](https://github.com/livekit/livekit/blob/v1.9.7/pkg/sfu/rtpmunger.go)には
直前のtimestampを使って新しい連番を発行する経路がある。
実測の80bytesはその固定無音frameのサイズと一致するが、本文一致を測定していないため、
実packetの発生元をこの一致だけで断定しない。

`acd5829`ではpacketの同一性を送信元・連番で検査し、timestamp・payloadも一致する場合だけ
重複再送として除外する。同じ連番の異なる内容は従来どおり拒否し、連番欠測も理由付き失敗とする。
別連番で同じ20ms区間が重なる場合は、独立decodeへの追加投入を止めて
`timestamp_overlap`として当該応答を中断する。確認済みplayed prefixだけを通知し、
停止／cancel／世代同期を送る。native frame配送は維持し、同じ音声区間を二度出力しない。
これは重複再送の件数やpacket欠落数に混ぜず、別の観測として保存する。

この変更後、実切断07・08では制御往復が復旧し、再生経路の致命的失敗も0件になったが、
次発話はSTT後に`response_failed`となった。`64b5aa0`で応答開始・配送失敗時の
例外型とコード上の失敗箇所だけを保存し、08では旧音声trackの公開解除における
`UnpublishTrackError`を確認した。例外本文と利用者入力の非露出はテストで検証した。

`ae578ec`では、SDKの公開一覧で既に消失した所有trackへ再度unpublishを送らない。
unpublish応答との競合でも、同じ所有SIDが一覧から消えた場合だけ旧sourceの解放へ進む。
SIDが残る場合や別種のエラーは失敗を保持し、新trackを追加しない。
修正前は消失・競合の再現テスト2件が失敗し、修正後はresponse audio／runtime audio／
Core lifecycle関連118件が成功した。

| run | 測定版 | 時計対応幅 | 制御回復上限 | 次発話 | session明示終了 |
|---|---|---:|---:|---|---|
| network-fault-session-06 | 38a8e64 | 1.676189ms | 未回復 | 失敗 | 確認済み |
| network-fault-session-07 | acd5829 | 1.724136ms | 2,432.170ms | 応答開始失敗 | 未確認 |
| network-fault-session-08 | 64b5aa0 | 1.700404ms | 4,583.698ms | 旧track解除失敗 | 未確認 |
| network-fault-session-09 | ae578ec | 1.774982ms | 986.382ms | 成功 | 確認済み |

全4runで専用bridgeの実切断・network復旧・切断中のcontrol不通を確認した。
07～09の観測済み出力は重複区間0、packet観測欠測0、再生経路エラー0だった。
ただし全4runとも回復測定10秒窓内の新規受信かつ可聴出力は得られず、
`audio_recovery_upper_ms`と両経路の`recovery_upper_ms`はnullで、総合結果は失敗である。

09では10秒窓を閉じた後、同じsessionの新しい発話・応答を確認した。
新応答は入力167,422 samples・padding1,538 samplesに対する168,960 samples全出力、
176packets、gap合計・最大gapとも0だった。旧trackの解除エラーは再発せず、
マイク再開も実STTから新応答の全再生まで検証できた。これは回復時間の代替値ではない。

全runの障害・時計子process終了、teardown、所有Frontend／Backend削除を確認した。
07・08のsession終了未確認はそのまま残し、コンテナ削除をsession正常終了の代わりにしない。
最後に専用LiveKitと専用networkの削除も検証した。失敗rawは各run rootへ保持する。
現Frontend全単体554件、型検査、build、Backend関連118件とRuffが成功した。
buildには既存の500KB超bundle警告が残る。

次は、切断時に旧応答を中断する既存方針と、再接続時に音声の利用可能性を実出力で確認する
測定条件を照合する。旧buffer・無音・接続フラグを音声回復へ置き換えない。
再接続100試行・99%・p95 3,000msは未達であり、VAD境界、stale、dogfood相当条件、
資源・RTP全分母、元設定の人格／記憶品質、最終実接続suite等も引き続き未完了である。

## 2026-09-07: 再接続100試行を再計算する集計器

#17・#112の現在の本文／コメントと`voice-session-contract-2026-08.md`を確認した。
復旧の終点はcontrol/audio双方の利用可であり、旧応答は実再生範囲まで中断し、
再送して途中から復元しない方針である。音声復旧の具体的な確認用入力は明記されていない。
音声経路の検査音による実出力確認と、次の会話応答の成功を分ける案を暫定方針として検討する。
この段階では検査音を実装・測定しておらず、旧応答bufferや無音を復旧へ数えていない。

`frontend/scripts/report-fault-recovery.mjs`と匿名report schemaを追加した。
全manifestの時計較正、実2秒切断、切断中の不通、制御往復、packet実出力から再計算し、
保存された`recovery`や合否フラグは流用しない。同一fixture・実装版、独立session／conversation、
試行抜け、終了確認、次発話の成功を照合する。音声未回復も失敗分母へ残し、
10秒以内に双方が復旧した全試行からp95を求める。全失敗のp95はnull、少数診断は不合格とする。
重複の観測範囲は現在のresponse／SSRC／RTP timestampのsample区間に限定される。
送信元変更を跨ぐsource PCM同一性の検証を、この集計の成功で代替しない。

runnerは測定前のcommitと未コミット変更を確認し、診断manifestへ実装版を記録する。
旧rawの版は後から推測して埋めず、欠測を保持する。既存の未追跡`backend/.venv` symlink以外に
未コミット変更がある場合は測定を始めない。依存runtimeと共有サービスの版は別の環境証跡で扱う。

集計・出力観測の関連21件、runner関連42件、型検査・Ruffが成功した。
99/100と98/100、成功時p95超過、全失敗、重複出力、復旧前の要求への遅着ACK、時計不整合、
試行・session・conversation・版の混在、匿名schemaを検証した。
既存出力の上書き拒否も、終了コード2とファイル不変で確認した。
初回の集計テストは時計幅の期待値に1件不一致があり修正した。
またschemaファイルの読み込みはDOM環境で失敗したため、純粋集計テストをNode環境に揃えた。

保存済み実測09をCLIで再計算し、制御986.382ms、音声回復0件、次発話成功1件を再確認した。
測定時のrawには実装版がないため`measurement_revision_unavailable`も残し、
総合結果は不合格のままとした。新たな実切断試験や100試行はこの工程では実行していない。
全#150の未完了条件とPR未作成の状態は変わらない。

## 再接続の実音声probeを追加（実測前）

旧応答を再開しない契約に合わせ、専用test Profileだけで固定音を送信し、音声transportが
利用可能になる時刻を測る処理を追加した。制御の復旧確認後に新しいnonceを発行し、同じ世代・
参加者・trackの受信、共通Opus復号、rendererの出力時計通過、全10,560 sampleを照合する。
通常の次発話成功も別途要求する。確認音で音声経路を測る境界は暫定で、ユーザーの回答を得たとは扱わない。

Backendの世代・送信元照合、既存会話音声、終了処理を含む149件が成功した。
Frontend全577件、追加のcohort／出力証跡35件、型検査・Ruff・ビルドが成功した。
集計の型検査でRTP timestamp変数の未確定型が1件見つかり、整数検証と型確定を追加して解消した。
この時点では新しい実接続試験はまだ実行していない。100再接続および#150全体の受け入れは未完了。

### 実障害10: 制御復旧と次発話は成功、診断音は未受信

`5f6fd92`で2秒の実切断を実施した。制御復旧は340.890ms、同sessionの次発話と再生完了は成功。
音声probeはtrack未確認・packet 0件で4秒のtimeoutとなった。音声復旧値はnull、欠測1、
全体は不合格として保持した。既存音声の出力経路失敗・重複区間は0件だった。
Backendの専用test Profile・計測種別・LiveKit URLは実際の起動環境でも有効条件と一致していた。
原因を切り分けるため、送信失敗ログに固定の処理段階を追加し、例外本文を記録しない。
session終了、障害操作子の終了、Frontend・Backendコンテナの実削除を確認済み。

### 実障害11: トラック公開後の準備待機でtimeout

`be3c056`でも音声probeは未受信となった。送信側の段階別ログは`ready / TimeoutError`で、
トラック公開後のBrowser準備通知を受け取れていない。制御復旧444.367ms、次発話成功、session終了・
障害操作子終了、Frontend・Backendの実削除を確認した。音声はnullのまま失敗分母に残した。
Browserの診断rawに、公開・購読イベントの時刻とnonce名・送信者の一致判定だけを追加した。
型検査とRoom関連34件が成功した。任意のtrack名・参加者名はこの追加記録へ含めない。

### 実障害12: Browserの公開・購読イベントが未到達

`4f12c58`では制御復旧976.982ms、次発話成功、音声probeはtimeoutで不合格となった。
診断の4秒間にBrowserのtrack公開・購読イベントは0件で、名前や送信者の照合前に止まっていた。
Backendはトラック公開後の準備待機でtimeout。session終了・障害操作子終了とFrontend・Backendの
実削除を確認した。遅れて到達するイベントを4秒で切り捨てないよう、診断全体10秒・準備待機8秒へ
変更した。障害回復起点の10秒／成功時p95 3,000msの評価基準は維持する。
送信元の既存track件数とSID再利用有無だけを追加診断し、Backend13件・Room34件が成功した。

### 実障害13: 診断音の全出力を確認、復旧時間は目標超過

`c399d91`で新しい診断音の公開・購読、nonce・送信者の一致、全10,560 sample／11 packetの
出力時計通過、gap 0、後始末を確認した。通常の次発話も成功した。
最初の制御応答の直後に世代が1から2へ進み、音声は世代2だった。集計が「同世代の制御応答は
必ず音声要求前」という余分な順序条件で弾いていたため、同世代の制御と実音声出力の双方が
成立した遅い時刻を採るよう修正した。旧世代の早いACKは復旧値へ採らない。

保存済みrawを再計算した結果は、同世代の制御5,226.118ms、音声・双方5,379.975msだった。
1件の10秒内復旧は証明できたが、3,000msを超えており受け入れ不合格。単独診断を100件の評価に
代用しない。匿名schemaで検証済みの`docs/artifacts/livekit-reconnect-audio-probe-pilot-v1.json`へ
結果を残した。初回のraw合否は失敗のまま保存し、再計算reportで測定版を推測変更していない。
集計・出力証跡36件と型検査が成功した。

実測10〜13はいずれもsession終了・次発話成功・障害操作子終了、所有するFrontend・Backendの
実削除を確認した。専用LiveKitとbridge networkも削除済みで、既存LiveKitの継続稼働を確認した。
制御の復旧直後に新規音声trackのイベントが届かない場合が残っており、復旧時間と再試行時の
世代管理は引き続き検証が必要。再接続100件、その他の#150受け入れ条件、PR作成は未完了。

## 再接続の再試行間隔と接続イベントの計測

導入済みLiveKit JS SDKでは、再接続後のPeerConnection確認に固定2秒の待機があり、標準の
再試行間隔も失敗回数に応じて1.2秒・2.7秒以上へ増える。短い障害の回復直後に再試行が遅れる
可能性を調べるため、公開`reconnectPolicy`で最初の10秒を250〜500ms、以降を1〜1.5秒とし、
最大40回・60秒で停止する方針を追加した。初回は即時、jitterと長時間障害のbackoffは残す。
SDKの固定待機や内部状態を上書きしない。この変更だけで目標達成とは扱わない。

診断時だけ、再試行の回数・待機時間、signal接続・再接続、state sync要求、Core世代の時刻を
Browserの同じ時計で記録する。URL・token・例外本文は残さない。関連41件と型検査が成功した。
実障害での効果はこれから検証する。

### 再試行間隔調整後の実障害14・15

`526c17a`の実障害14は制御2,213.875ms、音声・双方2,227.438ms、15は制御2,495.868ms、
音声・双方2,510.183msとなり、両試行とも3秒以内で成功した。診断音の全出力、gap・重複区間0、
同sessionの次発話成功、session・障害操作子の終了、Frontend・Backendの実削除を確認した。
14の接続時系列では短い再試行が複数回行われ、state sync後にSDKの再接続完了と世代更新を経て
新しい音声trackを購読した。2件の成功を100件の受け入れへ代用しない。

100独立sessionを逐次実行するrunnerを追加した。事前に全run IDと実装版を固定し、失敗した試行を
削除・再実行せずに記録する。各試行の実削除と障害操作子の終了を確認してから次へ進み、
実装版・専用コンテナの変更や未検証の後始末では停止する。全件終了後だけ匿名cohortを再集計する。

runner・既存pilotの関連61件、再接続方針・Roomの41件、型検査・Ruff・Frontendビルドが成功した。
初回Ruffでimport順とCLI境界の広すぎる例外捕捉を指摘され、対応した。失敗試行の保持、
後始末不明・版変更時の停止、同時cohortの排除、所有process groupだけの終了を検証した。
100試行は専用bridge、固定scheduled fixture、明示的なthinking無効条件で実行する。


### 初回100件計画を13件で停止、再接続中の競合を修正

`eec8bb3`の`reconnect-controlled-20260907-a`は13件終了後に停止した。
成功7件、失敗6件であり、残る87件は未実施。100件の受け入れ結果として扱わない。
全13件のsession終了・障害操作子終了と所有Frontend・Backendの実削除を確認し、
計画、全raw、終了journal、停止要求を保存した。専用LiveKitは次の診断用に維持している。

6・9件目は診断音の処理中に世代が更新された。RTP不連続後の早い状態同期とSDK再接続後の
状態同期が重なる経路があったため、SDK再接続中は同期要求を保留し、新世代の状態同期が
終わってから診断音を要求する。実際に開始した音声probeの失敗は削除・置換しない。
12件目は連番が正しいpacket間のRTP差が1,922 sampleとなり、接続全体が終了していた。
この時刻不連続では未出力PCMと当該応答を停止し、制御接続を維持する。欠落を出力済みに補完しない。
11件目の切断原因は未確定だが、コード上はACK送信失敗が記録なしの全切断へ進んでいた。
Core eventを一度だけ適用した後、ACKのIDだけを最大256件・60秒保持して再送するよう変更した。
受信・適用エラーの切断には、固定の診断stageを残す。

ACKの再送・重複・上限・期限・clearとの競合、SDK再接続中の同期保留と音声probe待機、
非整列RTP差の応答停止について、関連48件が成功。Frontend全単体612件、型検査
（エラー・警告0件）、ビルドも成功した。ビルドの既存chunkサイズ警告は残る。
修正後の実通信は未検証。次は独立したpilotで確認し、新しい計画IDで100件を評価する。


### 実障害16と第2計画: 状態同期の応答欠落を検出

`07996dc`の実障害16は、制御2,464.989ms・音声と双方2,595.323msで成功した。
診断音10,560 sample、次発話168,960 sampleの全出力、gap・重複0、欠測・出力経路エラー0を確認。
session・障害操作子の終了、所有Frontend・Backendの実削除も確認した。

続く`reconnect-controlled-20260907-b`は6件終了後に停止した。1・5・6件目が成功し、
2・4件目は双方の復旧3,259.511ms／4,855.567msで目標を超過した。3件目はSDK再接続後の
状態同期応答を観測できず、制御・音声・次発話が失敗した。全6件の終了と所有サービス実削除を
確認し、失敗rawを保持した。計画100件の残94件は未実施であり、合否集計へ補完しない。

Browserは状態同期要求を一度送るだけだったため、同じ要求世代を250ms間隔・最大60秒で
再送するよう変更した。送信Promiseが未完了でも期限は監視し、同時送信を増やさない。
新世代のavailable確認または終了で止める。実測の10秒回復窓・3秒p95基準は変えない。
Core ACKはSDK再接続完了待ちを理由に保留せず、一時的な送信失敗だけを再送する。

Backendは状態通知の後に世代準備を実行し、同じ要求の再送でも準備callbackを繰り返していた。
準備が終わる前の通知と同時再送の重複を2件の失敗テストで再現した。同期要求を直列化し、
マイクreader再開と旧probe停止が完了してから状態を通知する。準備済み世代の再送では
準備callbackを繰り返さず、準備失敗時は同じ世代で再試行する。準備中の世代変更を準備済みと補完しない。

関連Frontend53件、全単体617件、Backend単体・結合120件、型検査、Ruff、ビルドが成功した。
Frontendの初回2件失敗は非同期送信前に応答を注入するテストの順序を修正し、送信失敗・応答欠落の
回帰試験を追加した。新しい状態同期修正版の実通信検証はこれから行う。


### 実障害17: 状態同期の再送で復旧、3秒目標は未達

`3e707f3`で同じ世代0の状態同期要求を2回送信した後、世代1の状態を受信した。
制御2,954.154ms、音声・双方3,249.881msで、10秒内復旧を観測したが単独診断の3秒基準は不合格。
音声probeはcaptured、同sessionの次発話成功、重複・欠測・出力経路エラー0だった。
Core側では障害に伴う配送失敗も記録されており、正常会話の失敗率0の証拠として扱わない。
session・障害操作子・診断readerの終了、所有Frontend・Backend、専用LiveKitとnetworkの
実削除を確認した。通常LiveKitは継続稼働している。

SDK再接続完了13,956.8msの直後に状態同期を開始し、再送後14,366msに状態を受信していた。
次はSDKの再接続完了通知を待つ前から同じ世代の同期要求を再送し、確認済みの同期を
再接続完了時に繰り返さない構成を検討・検証する。公開SDK内部の固定待機を変更せず、
実control・実audioの因果的な復旧測定と100件の分母を維持する。


### signal再接続中から状態同期を開始する

SDKのSignalReconnectingを受けた時点から、同じ要求世代の状態同期を再送する。
要求世代より新しいavailable状態が返れば同期確認済みとし、実音声probeを開始できるようにした。
実control往復・実audio全出力の観測を省略するものではない。SDK Reconnectedが後から届いても、
確認済みの同期を再発行せず、進行中の同期も同じ要求世代で継続する。
再切断・full reconnect・明示接続では以前の同期確認とprobeを無効化する。
遅着した古い世代のauthoritative stateでは現在の世代を巻き戻さない。

SDK完了前の要求がないことを1件の失敗テストで再現し、修正後はACK・状態同期・Room関連54件が成功した。
再接続中のRTP停止と同期要求が重なる場合も、進行中の要求へ集約することを検証した。


### 実障害18: 制御は269msで復旧、音声probeは出力欠測

`e18a183`は制御269.292msで復旧した。同期確認は11,963.5ms、SDKの再接続完了は
14,252.4msであり、同期開始の遅れは短縮できた。新しい世代更新や同期の再発行はなかった。
一方、音声probeは12,077.4msに要求し、14,253.5msに購読イベントを受けた後、
出力packet証跡0件でtrack解除となった。Backendの完了ACK待機timeoutを記録した。
音声回復は欠測を保持し、単独診断は不合格。次発話、session・障害操作子・診断readerの終了、
所有Frontend・Backendの実削除は確認した。

probeのPCM送信はBrowserのreadyだけでなく、送信側LiveKitの`wait_for_subscription()`成立も
待つよう変更した。全体10秒の期限とcancel時の所有資源解放を維持する。購読前の送信が
今回の失敗原因と確定したわけではないため、Browserのprobeにも受信・復号の数値観測を残す。
Backend関連15件、Room・復旧集計71件、型検査、Ruff、ビルドが成功した。


### 実障害19: 音声の全出力を確認、状態同期の遅延が残る

`93c9b81`は音声probeの全10,560 sample／11 packetを出力し、受信・復号・実出力を別時刻で
確認した。gap・重複・欠測・出力経路エラー0、同sessionの次発話成功だった。
ただし制御4,371.823ms、音声・双方4,678.908msで3秒目標を超え、単独診断は不合格。
SDK再接続完了10,562.7msの後も同じ要求世代0の再送が続き、12,481.5msに世代1を受信した。
このBrowser側証跡だけでは、データ通信の遅延とBackendの準備・状態通知の待機を区別できない。
次は状態要求の受信、世代準備の開始・終了、状態送信の開始・終了の数値時刻を照合する。

実18・19のsession・障害操作子・診断reader終了と所有Frontend・Backendの実削除を確認済み。
専用LiveKitとnetworkも削除し、通常LiveKitの継続稼働を確認した。100件再接続の受け入れは未完了。


### 状態同期のBackend段階別診断

専用音声probeが有効なtest Profileだけ、状態要求の受信・lock取得、世代準備の開始・完了、
状態送信の開始・完了をBackend単調時計のms値で記録する。世代と固定stage以外の識別情報を
含めず、1 session最大512行とoverflow通知1行に制限する。Browser時計とは直接減算しない。
準備や送信で例外が発生した場合、完了stageを補完しない。Backend関連136件とRuffが成功した。


### 実障害C: 独立100件の結果と残る復旧遅延（2026-09-08）

測定版`77ab6521d72ec06f695818d7eb6a97c7068664fe`を固定し、同一のscheduled fixture、
thinking無効、独立session・conversationで100件の2秒断を最後まで実行した。
[匿名の再接続集計](artifacts/livekit-reconnect-100-2026-09-08-c.json)は不合格である。

- 全100件の記録、独立session・conversation、明示的session終了、障害操作子の終了を確認。
- 疎通回復から10秒以内に制御と音声の完全な復旧証跡を得たのは91件。
  未証明9件の内訳は障害の影響を確認できない2件、出力証跡が不完全な7件であり、
  全9件を通信そのものが復旧しなかった件数と同一視しない。
- 双方の復旧時間はp50 2,568.394ms、p95 5,481.955ms。99%以上・p95 3,000ms以下の目標は未達。
- 次の同session会話の完全出力は96件。観測したRTP sample区間の重複は0件だが、
  欠測のある試行まで重複なしと証明したことにはしない。
- 元の100 manifestから再集計し、保存reportとの完全一致とschema適合を確認。
  所有Frontend・Backend計200 containerの実削除、100診断readerの終了、専用SFUとnetworkの削除を確認。
  通常LiveKitは稼働を継続した。

段階別診断では、Backendの世代準備と状態送信の待機だけで秒単位の遅延を説明できなかった。
制御同期が先に終わり、SDK Reconnected前に診断音を要求した24・57件目は両方目標未達で、
57件目はpacket出力がなく、次会話だけ成功した。制御の利用可能性とmediaの準備完了を分ける必要がある。

修正では、状態同期を早く開始する動作を保ち、診断音の要求は状態同期とSDK再接続の両方の完了後に限る。
SDKの数値切断理由とアプリ側の切断起点を記録し、専用test ProfileではBackend自身のRTC接続と
音声publish・購読・ready・captureの段階も固定名・単調時計で記録する。ブラウザのsignaling診断には
HTTP statusと固定socketイベントだけを保存し、URLのquery、認証情報、本文を含めない。
Playwright側の時計はBrowser側と区別する。

修正後はFrontend単体627件、Backend関連単体146件、Backend全232 sourceの型検査、
Frontendの型検査、対象PythonのRuff、ビルドが成功した。ビルドには既存のchunkサイズ警告がある。
これらは実障害100件の合格を示すものではなく、修正版の実接続と再測定が必要である。


### 実障害21: 修正後もBackend SDKの復旧待ちが残る（2026-09-08）

`a5feaa6a7c363507c5ec59470d3c3287878d6c7c`の[単独診断](artifacts/livekit-reconnect-pilot-2026-09-08-21.json)では、
診断音10,560 sampleの全出力、次の同session会話、重複・欠測・出力経路エラー0を確認した。
しかし制御4,396.382ms、音声・双方4,679.080msで、3秒目標は未達である。

Backend単調時計で自身のRTC reconnectingからreconnectedまでは6,738msだった。
世代準備は0ms、状態送信は43msであり、今回の長い待機はこの準備処理だけでは説明できない。
音声要求の受付後はpublish 20ms、購読待ち163ms、ready確認後のcapture 216msだった。
Browser側もSDK Reconnectedからauthoritative state受信まで約1,876ms待っていた。
これらの異なる時計を直接減算せず、各時計内の区間として記録する。

SDK自身の復旧時間には障害が継続している時間も含まれるため、これを受け入れ指標の
「疎通回復後の時間」と置き換えない。HTTP診断には初回のv1接続試行時の404と、断中の接続失敗が
記録され、今回の断中に401/403は観測されなかった。過去のSDK最終切断の原因を確定する証拠ではない。

session・障害操作子・診断reader終了、所有Frontend・Backendの実削除、専用SFUとnetworkの削除を確認した。
次はBackend SDKの再接続開始から完了までの待機を調査し、実接続で改善を検証する。
