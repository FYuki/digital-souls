# Issue #150 受け入れ確認表

[Issue #150](https://github.com/FYuki/digital-souls/issues/150)のWorkstream A–Eと完了条件を対象とする。基準値は変更しない。以下は2026-09-09時点の保存証拠の確認であり、全条件の達成宣言ではない。[差分照合](artifacts/livekit-final-source-impact-2026-09-09.json)で測定時の版と変更範囲を記録した。過去の100件測定と現在版の少数回帰を区別する。

## main取り込み後の証拠の適用範囲（2026-09-09）

PR #269はepicへマージ済み。`main`の`dc30afd`までを取り込み、外部MCP・ツール利用・アドオン管理・生活状態runtimeを統合した。
音声の出力停止確認を待つ経路でもツールへ中断を一度だけ通知し、生成へ渡す最終prompt（ツール結果・生活状態を含む）を診断値へ記録する。
以下の100件測定と差分照合artifactは、このmain統合より前の版の証拠として保持する。Core・prompt構築・推論対象が変更されているため、過去の差分なしという判定を統合後のHEADへ引き継がない。
統合の自動回帰と、統合後の実声・性能受け入れは別に扱う。統合後の実声dogfoodや100件の性能再測定は未実施。

## 完了条件と証拠

| 条件 | 保存証拠と現在の評価 | 残る確認 |
|---|---|---|
| TTFA p95 ≤ 2,000ms、p50 ≤ 1,000msは改善目安 | [最新通常100件](artifacts/livekit-normal-isolated-100-2026-09-08-01-verification.json): p95 1,787.520msで上限より212.480ms短く達成。p50 1,736.350msは目安より736.350ms長い。前回11,995.305msの未達runも保持 | 共有Ollamaのcontext切り替えは今回0回。専有条件や将来の安定性を保証した結果ではない |
| utterance確定 p95 ≤ 800ms | 同じ最新通常100件: 289.539ms | 配備後の実声条件とは区別 |
| local playback stop p95 ≤ 3,000ms | [take-turn 100件](artifacts/livekit-take-turn-output-stop-proof-100.json): 1,936.250ms | [差分照合済み](artifacts/livekit-final-source-impact-2026-09-09.json)。現在版で100件を再測定したという意味ではない |
| turn decision p95 ≤ 3,000ms | 同じtake-turn: 1,935.300ms | [差分照合済み](artifacts/livekit-final-source-impact-2026-09-09.json)。現在版で100件を再測定したという意味ではない |
| decision後cancel p95 ≤ 200ms | 同じtake-turn: 30.266ms | [差分照合済み](artifacts/livekit-final-source-impact-2026-09-09.json)。現在版で100件を再測定したという意味ではない |
| speech startからcancel p95 ≤ 3,500ms | 同じtake-turn: 1,968.250ms | [差分照合済み](artifacts/livekit-final-source-impact-2026-09-09.json)。現在版で100件を再測定したという意味ではない |
| 比較可能なlatencyはbaseline + max(10%, 50ms)以内 | [最新100件](artifacts/livekit-normal-isolated-100-2026-09-08-01-latency.json): 比較可能8指標達成、観測点が異なる5指標は監査付き比較対象外。TTFA・発話確定の絶対上限も達成、通常latency比較判定true | 全Issueの合格とは区別。旧版の未達結果を保持 |
| 発話冒頭・早期終了・文中無音の誤分割が各1%以下 | [文中休止100件](artifacts/livekit-vad-pcm-multi-seed-v4-pause-100-2026-09-08.json): 各0件。[最新通常100件のnative検出境界](artifacts/livekit-normal-isolated-100-2026-09-08-01-boundaries.json)も開始遅延・早期終了0/100、欠測0。[実STT入力端部](artifacts/livekit-pcm-multi-seed-v4-pause-100-2026-09-08.json)100/100 | ブラウザ検出境界と実STT入力端部を混同しない。通常runの境界を文中休止の誤分割証拠に置き換えない |
| 相槌の誤cancel率 ≤ 2%、100件以上 | [相槌100件](artifacts/livekit-backchannel-100-2026-09-08-held.json): 誤cancel 0/100。意図不確定20件とharness失敗20件を保持 | 意図不確定を相槌分類成功と扱わず、誤cancelの観測範囲を保持。分類器・検出器の差分なしを確認 |
| take-turn見逃し率 ≤ 1%、100件以上 | take-turn 100件: 見逃し0/100、注入・決定・取消を全件確認 | [差分照合済み](artifacts/livekit-final-source-impact-2026-09-09.json)。現在版で100件を再測定したという意味ではない |
| reconnect 10秒以内 ≥ 99%、p95 ≤ 3,000ms、重複0 | [実障害100件](artifacts/livekit-reconnect-controlled-f.json): 100/100、2,683.880ms、重複0、次応答完全再生100/100 | [終了検証](artifacts/livekit-reconnect-controlled-f-verification.json)のharness成功96/100と独立再集計を区別。[差分照合済み](artifacts/livekit-final-source-impact-2026-09-09.json)。現在版で100件を再測定したという意味ではない |
| 制御測定のunderrun 0件 | 先行通常100件で1件・41.333ms、未達。[最新通常100件](artifacts/livekit-normal-isolated-100-2026-09-08-01-verification.json)は0件でTTFAも達成。[PCM中継条件100件](artifacts/livekit-normal-pcm-supply-100-2026-09-08-01-verification.json)は0件 | 最新の通常100件で条件達成。先行runの原因を修正済みとは主張せず、過去の未達証拠を保持する |
| dogfood gap合計 ≤ 0.1%、最大連続gap ≤ 200ms | 製品側の完全再生観測とgap集計を実装。実声dogfoodの証拠なし | 所定の配備後の実声測定 |
| stale presented 0件 | [旧応答提示100件](artifacts/livekit-stale-output-stop-proof-100.json): 音声・画面text・履歴textの提示0。受信は4件で最大4 packetsの可能性を保持 | 受信0と読み替えず、[差分照合済み](artifacts/livekit-final-source-impact-2026-09-09.json)。現在版で100件を再測定したという意味ではない |
| 通常100件の処理失敗0 | 最新通常100件で0/100、105件のnative再生・RTP・session終了を照合、所有FE/BE撤去済み | 実声dogfoodとは別条件 |
| dogfood処理失敗率 ≤ 1%、予期しないsession終了0 | 無応答を含むnative session journalと匿名集計を実装。最新通常100件の終了記録は欠測0・予期しない終了0・追加操作0。実声dogfoodの証拠なし | [無応答の正常終了・切断後終了2件](artifacts/livekit-zero-response-session-2026-09-08-02.json)を確認。[復帰後の記録1件](artifacts/livekit-reconnect-session-regression-2026-09-08-03-verification.json)も欠測0。実声受け入れが残る |
| session開始後3往復以上の追加操作0 | [製品session計測の実接続3往復](artifacts/livekit-native-session-three-turn-2026-09-08-01-verification.json): 1 session・完全再生3応答・追加操作0・正常終了1・欠測0。ブラウザ操作観測とnative journalを照合 | 配備後の実声dogfood確認 |
| #17必須指標に未実装由来のmissingを残さない | 製品RTP経路、再生、割り込み、手動resource入力を実装。欠測理由と分母は保持 | 取消応答の通信観測は[実接続3件](artifacts/livekit-cancelled-network-pilot-2026-09-09-01.json)で照合済み。実声dogfoodを確認。無応答の終了記録2件・復帰後の記録1件は実接続照合済み。全指標完了は未証明 |

## Workstreamごとの実装・測定範囲

- A: 音声latency policyとprovider診断を実装。Ollamaの内部queue・生成開始時点はAPIが公開せず、理由を記録する。model loadや残差をqueue待ちと推定しない。[文脈量比較](artifacts/livekit-context-comparison-2026-09-08.json)は48要求、[内容確認](artifacts/livekit-context-answer-content-2026-09-08.json)は36件中28件成功（thinking無効18/18）。実RAG取得や自然な人格品質の証明とは分ける。
- B: 受信・復号・実出力時計、割り込みのsession/utterance/旧response相関、正常cancelと処理失敗の分離を実装。製品traceとmanifestの一致を各reporterで検証する。
- C: 相槌・take-turn・文中休止の独立100件と、生成／受信・提示の区別を実装。実PCMのv4端部照合は前後最大600msの候補を使い、内部全体や全帯域の品質は主張しない。正常301音声と8種類の破損を用いた校正を別保存している。
- D: 専用network障害・復旧、再生継続性、resource、RTPの観測経路を実装。[製品RTP pilot](artifacts/livekit-native-network-pilot-2026-09-08-01-verification.json)で準備を含む4応答のnative trace一致を確認。完了応答の音声RTP payloadが範囲であり、session全通信量ではない。session追加操作・終了の製品集計を実装し、実接続3往復で追加0・正常終了・欠測なしを確認。無応答の正常終了と切断後の猶予満了終了を実接続2件で照合した。[一時切断から復帰したsession](artifacts/livekit-reconnect-session-regression-2026-09-08-03-verification.json)も追加0・正常終了・欠測0を照合した。実声dogfoodが残る。
- E: 固定fixture・初期状態・warm-up除外・独立session/conversationと匿名schema検証を実装。失敗runを残す。[現在版の自動回帰](artifacts/livekit-automated-regression-2026-09-08-06.json)は全ユニット・モジュール・mocked E2E・型検査・lint・buildを通過。[実サービス音声回帰5ケース](artifacts/livekit-voice-regression-suite-2026-09-08-04.json)も成功。取消応答の通信観測修正後も[結合・モックE2E・型検査等](artifacts/livekit-cancelled-network-regression-2026-09-09.json)と実接続3件を検証。PR #269のCI全4ジョブは通過し、epicへマージ済み。実声dogfoodの全条件は未完了。

## 差分照合後に残る受け入れ

取消応答の通信観測は`579d190`で修正し、実LiveKit／Whisper／Ollama／VOICEVOXを使った独立3 sessionで、
native取消記録と送受信量・packet lossを照合した。対象3応答の欠測は0、所有Frontend／Backendの撤去を確認した。
通常100件のTTFA、相槌・割り込み・再接続の100件評価を、この3件で置き換えない。

1. 人による実声dogfoodで、gap合計0.1%以下・最大200ms以下、処理失敗率1%以下、予期しない終了0、3往復の追加操作0を測定する。自然な応答・人格・記憶境界も既存の受け入れ手順で確認する。現時点では実測証拠がなく、達成扱いにしない。
2. 実声runの欠測理由・分母を照合する。Ollama APIが公開しない内部受付・生成開始・queue時間は理由付き欠測とし、実装済み指標や0msと主張しない。完全再生／取消時点のRTP payloadと、session全体の通信量を区別する。
3. main統合後の自動検証を確認し、epic→mainの差分レビュー・修正とユーザーによるマージ、明示deployを経て、上記の実声測定へ進む。PR #269のマージやepicの最新化だけで#150を完了にしない。

先行runの41.333msの音切れとTTFA p95 11,995.305msの未達は保持する。最新通常100件の合格を将来の安定性や共有Ollamaの専有保証には使わない。一方、元Issueにない専有証明や過去全runの原因解決を追加の受け入れ条件にはしない。

自動テストはtest Profile/data rootだけを使用する。dogfoodのFrontend・Backend・DBへテストを実行せず、実声受け入れは配備手順と人による確認を必要とする。手順は[音声品質測定](voice-quality-measurement.md)、[テスト方針](testing-policy.md)、[リポジトリ方針](repository-policy.md)を参照。
