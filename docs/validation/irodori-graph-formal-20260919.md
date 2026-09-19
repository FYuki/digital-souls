# Irodori高速化後の空状態100独立試行（2026-09-19）

第1段階の速度条件は **未達**。利用者発話終了→実ブラウザ出力開始の
p95は **2001.30ms**（上限2,000ms）、p50は **1837.25ms**。
これは空状態・固定音声での結果であり、#350/#423/#424全体の受入完了ではない。

[既存reporterの出力](../artifacts/irodori-graph-formal-20260919/report.json)、
[全105件の匿名証跡](../artifacts/irodori-graph-formal-20260919/evidence.json)、
[準備・終了集計](../artifacts/irodori-graph-formal-20260919/summary.json)、
[原証跡と公開ファイルのhash](../artifacts/irodori-graph-formal-20260919/manifest.json)を保存した。

集計の初回起動はPYTHONPATH漏れで失敗した。元summaryと失敗logのhashを保持し、
同じ固定版のreporterへPYTHONPATH=backendを渡して別ファイルへ再集計した（exit 0）。
音声試行の追加・差替えや集計式の変更はない。起動修正は[PR #448](https://github.com/FYuki/digital-souls/pull/448)へ分離した。
p95の時計区間は2,000msをまたぐため、丸めや反対側の端点で合格に読み替えない。

## 事前固定した条件

- cohort: 350-irodori-graph256-full-0919-01。準備5回＋測定100回を事前登録し、全105回を完走した。
  各試行で新規data rootと独立した会話・Sessionを使用。後から試行の除外・差替えを行っていない。
- BE/FE/runner: 93c8aa7dd569f991e872de5ac75502e8e2111ccb。
  Backend image: sha256:36489665e750e9301e5c786d38b90ce179dd7a117a0237b7978044fa5dd2aa14。
  Frontend image: sha256:0d6e0c6dadf166357e78bcdcfc8c277d2245ac164557ecb1bbc47a9e7d493ac8。
- TTS: 59a7d49157e60867b399b0b2b9e2e4fb46d13a68、
  image sha256:1584e3832accdc66e1ad91a96d386a1f14e872a8badf5e844d6837fe4ff136e0。
  CUDA Graph有効、入力上限256MiB、最大2形状。compile無効。
- miori-b3-4221、B3 caption、seed 4221、40 steps、speed 1.02、BF16。
  duration・sampling schedule・出力保護を変更していない。
- 専用Ollama 0.34.2（11534）と専用TTS（50026）、integration-irodori-cuda-graph。
  gemma4:e4b、context 8,192、測定時thinking無効。モデル常駐等の観測は試行別に保存した。
- speech-v2、入力SHA256 56e10d861de9fdb8e9057d87c551432e4fea06656f8a60b88b4701929ce6722e、
  48kHz、正解発話は「こんにちは」。
- 履歴・記憶は開始時に全対象table 0行、RAG無効。
  実起動receiptで形成・統合schedulerの停止を確認し、各初期状態hashを照合した。
- 共有Ollama/TTS・dogfoodの設定とデータは変更していない。
  実行中に候補サービスを再起動したり、測定worktreeを変更したりしていない。

## 速度・全分母・時計

| 指標 | 結果 |
|---|---:|
| 測定予定 / 記録済み | 100 / 100 |
| TTFA成功 / 分母 | 100 / 100 |
| TTFA失敗 / 欠測 / 除外 | 0 / 0 / 0 |
| TTFA p50 | 1837.25ms |
| TTFA p95 | 2001.30ms |
| 時計区間を反対側で計算したp95 | 1999.55ms |
| 全105回の処理失敗 | 0 |
| 測定側gapあり / 準備側gapあり | 0 / 0 |

TTFAは同一browser時計の発話終了PCM境界→AudioWorklet出力開始を使用した。
発話終了の因果下限を使い、時計誤差により遅延を過小評価しない。
集計は既存type-7 p95とevaluate_latency_targetを使用した。
manifest内の送信時刻基準latencyMsをTTFAに読み替えていない。
準備時間、別時計の工程時間、過去のpilotを正式分母へ混ぜていない。

## 準備と工程内訳

開始準備はクリック→マイクstandbyのDOM観測で、接続・入力認可等を含む。
全体の時間上限による受入判定は行わず、個別の失敗と欠測を保存する。

| phase | 準備確認 / 予定 | p50 ms | p95 ms |
|---|---:|---:|---:|
| warmup | 5 / 5 | 1362.90 | 1795.80 |
| measured | 100 / 100 | 1378.80 | 1911.60 |

工程内訳は同一server時計内の差分。重なりや工程外の待ちがあるため、合計をTTFAと同一視しない。

| 工程 | 観測数 | p50 ms | p95 ms |
|---|---:|---:|---:|
| STT | 100/100 | 138.61 | 184.47 |
| prompt準備 | 100/100 | 144.06 | 170.78 |
| LLM request→first token | 100/100 | 41.11 | 48.29 |
| 先頭TTS | 100/100 | 565.85 | 666.61 |

## 再生とリソースの限界

全試行の予定sample数・実出力sample数・gapを匿名証跡へ保存した。
試行ごとの所有Backendについて、既存aggregate_resourcesでCPU・memoryを集計した。
別コンテナの累積CPU counterを一本に連結していない。
起動前／終了後などの欠測理由も残した。
GPUは共有hostを低頻度で観測した値であり、TTS単体のpeak VRAMやVRAM削減率の証拠ではない。
report.json単独のresource欠測欄を数値で埋め替えず、試行別証跡を併読する。

実再生の観測は固定音声による自動試験。人の実マイク・聴感、読み・声質・抑揚の
同等性を自動承認するものではない。
所有BE/FEは各試行終了時に停止した。専用推論サービスは後続検証用に保持している。

## 統合と残条件

製品変更#446と先行診断証跡#447は、それぞれ固定headの全CI成功後にEpicへ統合した。
その後のEpic CI / Container imagesはGitHubの課金・利用上限に関するエラーで
ジョブ開始前に失敗した。統合後CI成功やイメージ公開済みとは扱わない。
mainへのマージは行っていない。

第2段階の固定テスト記憶参照は未実施。第1段階達成を確認した場合にのみ続行し、
参照側のp50/p95・差分・失敗・欠測を記録するが、受入閾値は置かない。
記憶形成の性能調査へ拡張しない。
再接続・相槌・文中休止・TTS失敗後のブラウザ回復・実マイク/聴感・更新/切り戻し・
未完了レビュー等は[残受入対応表](voice-quality-350-423-424-status.md)の条件を継続する。
