# Issue #150 受け入れ確認表

[Issue #150](https://github.com/FYuki/digital-souls/issues/150)のWorkstream A–Eと完了条件を対象とする。基準値は変更しない。以下は2026-09-08時点の保存証拠の確認であり、全条件の達成宣言ではない。測定revisionが異なる結果は、最終変更の影響範囲との照合が必要である。

## 完了条件と証拠

| 条件 | 保存証拠と現在の評価 | 残る確認 |
|---|---|---|
| TTFA p95 ≤ 2,000ms、p50 ≤ 1,000msは改善目安 | [通常100件](artifacts/livekit-voice-default-controlled-100-2026-09-08-verification.json): p95 1,845.425msの達成runあり。ただし[最新100件](artifacts/livekit-normal-supply-100-2026-09-08-01-verification.json)はp95 11,995.305msで9,995.305ms超過、p50 1,768.650msで目安未達 | 想定外contextとprovider load増大を切り分け、再現性を確認 |
| utterance確定 p95 ≤ 800ms | 同じ通常100件: 299.950ms | 最終変更との照合 |
| local playback stop p95 ≤ 3,000ms | [take-turn 100件](artifacts/livekit-take-turn-output-stop-proof-100.json): 1,936.250ms | 最終変更との照合 |
| turn decision p95 ≤ 3,000ms | 同じtake-turn: 1,935.300ms | 最終変更との照合 |
| decision後cancel p95 ≤ 200ms | 同じtake-turn: 30.266ms | 最終変更との照合 |
| speech startからcancel p95 ≤ 3,500ms | 同じtake-turn: 1,968.250ms | 最終変更との照合 |
| 比較可能なlatencyはbaseline + max(10%, 50ms)以内 | [通常比較](artifacts/livekit-voice-default-normal-latency-2026-09-08.json): 8指標達成、観測区間が異なる3指標は比較対象外。VAD境界2指標欠測により全体判定false | VAD境界の証拠と比較可能性の確認 |
| 発話冒頭・早期終了・文中無音の誤分割が各1%以下 | [文中休止100件](artifacts/livekit-vad-pcm-multi-seed-v4-pause-100-2026-09-08.json): 各0件。[実STT入力端部](artifacts/livekit-pcm-multi-seed-v4-pause-100-2026-09-08.json)100/100 | ブラウザ検出境界と実STT入力端部を混同しない。通常runの欠測とは別に扱う |
| 相槌の誤cancel率 ≤ 2%、100件以上 | [相槌100件](artifacts/livekit-backchannel-100-2026-09-08-held.json): 誤cancel 0/100。意図不確定20件とharness失敗20件を保持 | 意図不確定を相槌分類成功と扱わず、誤cancelの観測範囲と最終変更を照合 |
| take-turn見逃し率 ≤ 1%、100件以上 | take-turn 100件: 見逃し0/100、注入・決定・取消を全件確認 | 最終変更との照合 |
| reconnect 10秒以内 ≥ 99%、p95 ≤ 3,000ms、重複0 | [実障害100件](artifacts/livekit-reconnect-controlled-f.json): 100/100、2,683.880ms、重複0、次応答完全再生100/100 | [終了検証](artifacts/livekit-reconnect-controlled-f-verification.json)のharness成功96/100と独立再集計を区別。最終変更との照合 |
| 制御測定のunderrun 0件 | 先行通常100件で1件・41.333ms、未達。最新供給診断100件は0件だがTTFA未達。[PCM中継条件100件](artifacts/livekit-normal-pcm-supply-100-2026-09-08-01-verification.json)は0件 | 通常経路で原因調査。別条件で再発しないことだけでは修正済みにしない |
| dogfood gap合計 ≤ 0.1%、最大連続gap ≤ 200ms | 製品側の完全再生観測とgap集計を実装。実声dogfoodの証拠なし | 所定の配備後の実声測定 |
| stale presented 0件 | [旧応答提示100件](artifacts/livekit-stale-output-stop-proof-100.json): 音声・画面text・履歴textの提示0。受信は4件で最大4 packetsの可能性を保持 | 受信0と読み替えず、最終変更との照合 |
| 通常100件の処理失敗0 | 通常100件で0/100 | 最終runとの照合 |
| dogfood処理失敗率 ≤ 1%、予期しないsession終了0 | 無応答を含むnative session journalと匿名集計を実装。実声dogfoodの証拠なし | 実障害時のsession記録と実声受け入れ |
| session開始後3往復以上の追加操作0 | [製品session計測の実接続3往復](artifacts/livekit-native-session-three-turn-2026-09-08-01-verification.json): 1 session・完全再生3応答・追加操作0・正常終了1・欠測0。ブラウザ操作観測とnative journalを照合 | 配備後の実声dogfood確認 |
| #17必須指標に未実装由来のmissingを残さない | 製品RTP経路、再生、割り込み、手動resource入力を実装。欠測理由と分母は保持 | VAD境界、取消途中の通信範囲、実障害時のsession記録を確認。全指標完了は未証明 |

## Workstreamごとの実装・測定範囲

- A: 音声latency policyとprovider診断を実装。Ollamaの内部queue・生成開始時点はAPIが公開せず、理由を記録する。model loadや残差をqueue待ちと推定しない。[文脈量比較](artifacts/livekit-context-comparison-2026-09-08.json)は48要求、[内容確認](artifacts/livekit-context-answer-content-2026-09-08.json)は36件中28件成功（thinking無効18/18）。実RAG取得や自然な人格品質の証明とは分ける。
- B: 受信・復号・実出力時計、割り込みのsession/utterance/旧response相関、正常cancelと処理失敗の分離を実装。製品traceとmanifestの一致を各reporterで検証する。
- C: 相槌・take-turn・文中休止の独立100件と、生成／受信・提示の区別を実装。実PCMのv4端部照合は前後最大600msの候補を使い、内部全体や全帯域の品質は主張しない。正常301音声と8種類の破損を用いた校正を別保存している。
- D: 専用network障害・復旧、再生継続性、resource、RTPの観測経路を実装。[製品RTP pilot](artifacts/livekit-native-network-pilot-2026-09-08-01-verification.json)で準備を含む4応答のnative trace一致を確認。完了応答の音声RTP payloadが範囲であり、session全通信量ではない。session追加操作・終了の製品集計を実装し、実接続3往復で追加0・正常終了・欠測なしを確認。実障害時の記録照合と実声dogfoodが残る。
- E: 固定fixture・初期状態・warm-up除外・独立session/conversationと匿名schema検証を実装。失敗runを残す。現在HEADの全体CI、必要な実接続回帰、全条件の最終照合とPR作成は未完了。

## 次の作業

1. 最新100件の速度再悪化を調査する。TTFA 2秒超過29件のうち28件でOllama load 1秒超過を確認した。他の要求元は未特定であり、共有推論を停止・変更せず、context変化との関係を切り分ける。
2. 診断結果から音切れの供給遅れを評価し、必要な修正と受け入れ測定を行う。
3. 実装したsession journalを、実障害・無応答の実接続条件でも照合する。無応答・cleanup失敗・記録欠落・逆行する累積操作は関連ユニットテストで確認済み。正常な実接続3往復はnative完全再生3件・追加0・正常終了1・欠測0。
4. VADの観測境界とbaseline比較を照合し、必須指標の未実装欠測を解消する。
5. 最終変更に必要な自動・実接続回帰を実行し、配備後の実声受け入れとPRを所定の順序で進める。

自動テストはtest Profile/data rootだけを使用する。dogfoodのFrontend・Backend・DBへテストを実行せず、実声受け入れは配備手順と人による確認を必要とする。手順は[音声品質測定](voice-quality-measurement.md)、[テスト方針](testing-policy.md)、[リポジトリ方針](repository-policy.md)を参照。
