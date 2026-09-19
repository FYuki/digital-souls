# 参照音声latent再利用後の空状態100独立試行（2026-09-19）

空状態の第1段階は **達成**。利用者発話終了から実ブラウザ再生開始までの
p95は **1967.65ms**（上限2000ms）、p50は **1796.15ms**。
測定100/100件が成功し、失敗・欠測・除外は0件。準備5件を含む全105件で
予定sample数と実出力sample数が一致し、再生gapは0、Session終了とnative SDK verifiedを確認した。
#350/#423/#424全体の受入完了ではない。

[既存reporter出力](../artifacts/irodori-reference-formal-20260919/report.json)、
[全105件の匿名証跡](../artifacts/irodori-reference-formal-20260919/evidence.json)、
[準備・終了集計](../artifacts/irodori-reference-formal-20260919/summary.json)、
[原証跡のhash](../artifacts/irodori-reference-formal-20260919/manifest.json)を保存した。

## 固定条件と前段からの変更

- cohort: 350-irodori-reference-full-0919-01。準備5件と独立100試行を事前登録し、全件を記録。
  各試行は新規data root、独立した会話とSession。試行の除外・差替え・追加はない。
- BE/FE/runner: 93c8aa7dd569f991e872de5ac75502e8e2111ccb。
  試行ごとの実image IDを匿名証跡に保存した。同じsource tagでもimage IDは試行間で異なる。
- TTS: 0b97ed943aa660877dccc24b8df25cae6eec1549、
  image sha256:d8279a7a43adc105ebe14b4bd3620fe2839c44966ff756c8c93e5dde9fba916c。
  CUDA Graph有効、入力上限256MiB、最大2形状、参照latent cache有効、compile無効。
  製品変更は[PR #450](https://github.com/FYuki/digital-souls/pull/450)へ分離。
- 専用Ollama 0.34.2（11534）、専用TTS（50026）、integration-irodori-cuda-graph。
  gemma4:e4b、context 8192、測定時thinking無効。
- miori-b3-4221、採用B3 caption、seed 4221、40 steps、speed 1.02、BF16。
  duration、sampling schedule、声、出力保護は変更していない。
- speech-v2、48kHz、入力SHA256
  56e10d861de9fdb8e9057d87c551432e4fea06656f8a60b88b4701929ce6722e。
  正解発話は「こんにちは」。固定WAVによる実STT/LLM/TTS/LiveKit/ブラウザ試験である。
- 履歴・記憶の全対象tableが開始時0行、RAG無効。形成・統合scheduler停止と初期状態hashを確認。
- reporter起動時にPYTHONPATH=backendを明示。既存の固定reporterでexit 0。
  [#448](https://github.com/FYuki/digital-souls/pull/448)の起動修正とは分離し、計測式は変えていない。

前段のGraph256のみの100件はp95 2001.30msで未達だった。
[#449の原記録](https://github.com/FYuki/digital-souls/pull/449)を維持し、
参照latent再利用という実装変更後の別cohortとして本結果を記録する。
前後は同時の対比較ではないため、p95の差全体をcache単独の効果とは断定しない。
TTS単体のON/OFF対比較は[別記録](irodori-reference-cache-20260919.md)を参照。

## 全分母と計測境界

| 項目 | 結果 |
|---|---:|
| 測定予定 / 記録済み | 100 / 100 |
| TTFA成功 / 分母 | 100 / 100 |
| TTFA失敗 / 欠測 / 除外 | 0 / 0 / 0 |
| TTFA p50 | 1796.15ms |
| TTFA p95（保守側） | 1967.65ms |
| 反対側の時計端点でのp95 | 1966.39ms |
| 準備を含む処理成功 / 全分母 | 105 / 105 |
| 測定側 / 準備側のgapあり試行 | 0 / 0 |

同一browser時計の発話終了PCM境界からAudioWorklet出力開始までを使う。
発話終了の因果下限を採る保守側の値を既存type-7 quantileとevaluate_latency_targetへ渡した。
送信時刻基準latencyMs、準備時間、別時計の工程時間をTTFAへ混ぜない。
既存の中央値目安は未達（median_guide_met=false）だが、合意した第1段階の受入条件はp95と失敗・coverageである。
他の条件まで合格へ拡張しない。

## 準備と工程内訳

準備は開始クリックからマイクstandbyまでのDOM観測。TTFAから分離し、全体上限は設けない。

| phase | ready / 分母 | p50 ms | p95 ms | 最大 ms |
|---|---:|---:|---:|---:|
| warmup | 5 / 5 | 1382.60 | 1900.50 | 1900.50 |
| measured | 100 / 100 | 1386.50 | 1913.80 | 2319.60 |

候補TTSサービスの起動は13.09秒程度、モデル準備は11.82秒。
測定中の候補サービス再起動はなく、ブラウザ準備時間とは別に記録した。

以下はserver時計内の差分で、全工程100/100件。重複区間や工程外の待ちを含むため、合計をTTFAと同一視しない。

| 工程 | p50 ms | p95 ms |
|---|---:|---:|
| STT | 141.62 | 190.08 |
| prompt準備 | 138.17 | 162.80 |
| LLM request→first token | 42.19 | 49.61 |
| 先頭TTS | 522.27 | 644.76 |

## 再生・resource・残条件

試行ごとの所有Backendについて既存aggregate_resourcesでCPU・memoryを集計した。
別コンテナの累積CPU系列は連結せず、欠測理由も保持した。
GPUは共有hostの低頻度観測で、TTS単体のpeak VRAMを示さない。
計測中に短いCPU単体検査とソース編集を行っており、host全体が無負荷だったとは主張しない。
競合するGPU測定は起動していない。

所有BE/FEは全試行で停止。正式cohortの終了とprocess消滅を確認後、
測定用TTSコンテナだけをIDと所有ラベル照合して回収し、後続の故障試験用コンテナに分離した。
共有サービス・dogfoodの設定とデータは変更していない。

第2段階は固定テスト記憶の参照のみを追加測定し、p50/p95・差分・失敗・欠測と実参照の証拠を記録する。
速度の受入閾値は置かず、記憶形成の影響調査を追加しない。現時点ではNOT_RUN。
人の実マイク・聴感・読み・声質の同等性、再接続・相槌・文中休止・失敗回復・
一括更新/切り戻し・未完了レビューは[残受入対応表](voice-quality-350-423-424-status.md)に従って継続する。

製品PRと本測定PRは別。GitHub CIは課金・利用上限に関する開始前失敗が継続しており、
ローカル実測成功をCI成功へ読み替えず、Epicへ未統合。mainへのマージはユーザーが実施する。
