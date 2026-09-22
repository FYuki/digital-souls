# 固定記憶参照20独立試行（2026-09-19）

第1段階の空状態受入後、固定合成記憶の参照だけを追加測定した。
準備5件＋測定20件が **25/25成功**。測定20/20で固定記憶3件のプロンプト参照を確認し、
全25件でsample数一致・gap 0、実native SDK、Session終了、専用環境teardownを確認した。
これは閾値なしの追加計測であり、全音声品質受入を意味しない。

基盤は[PR #454](https://github.com/FYuki/digital-souls/pull/454)、全実測証跡は本測定PRへ分離した。
[集計](../artifacts/voice-memory-reference-20260919/report.json)、
[準備等のsummary](../artifacts/voice-memory-reference-20260919/summary.json)、
[全25件](../artifacts/voice-memory-reference-20260919/evidence.json)、
[原記録・code hash](../artifacts/voice-memory-reference-20260919/manifest.json)を保存した。
[先行診断3run](voice-memory-reference-pilot-20260919.md)の準備失敗・観測欠測は別に保持する。

## 比較結果

| 指標 | 空状態100件 | 固定記憶参照20件 | 差 |
|---|---:|---:|---:|
| 発話終了→実再生 p50 | 1796.50ms | 2190.43ms | +393.93ms |
| 発話終了→実再生 p95 | 1968.00ms | 2338.78ms | +370.79ms |
| 失敗／欠測／除外 | 0／0／0 | 0／0／0 | — |
| 記憶参照 | 無効 | 20/20で3件 | — |

[時計精度の補正](voice-clock-precision-20260919.md)後の値。元reportは当時の出力として保持した。
時計区間の保守側、Hyndman–Fan type 7で比較する。段階別p95を加算しない。
独立した時点・異なる試行数のcohort比較であり、対応ペアや信頼区間付きの因果効果とは主張しない。
今回の記憶集合3件から大量記憶や他の問い合わせへの一般化は行わない。

測定20件のSession開始操作→マイクstandbyはp50 1365.60ms、p95 1875.10ms、最大1892.50ms。
準備5件の初回を含む最大3389.70msも保持する。これはDOM観測の別指標で、
記憶fixture登録・index生成・アプリ起動など、開始操作前の事前準備は含まない。
事前準備の成否と時刻は各runのenvironment report、index状態、host記録に残す。

## 固定条件と参照証拠

- cohort: 350-memory-reference-full-0919-01、計測版093b4238406a02bc64513d47c8d527b6de06d05e。
- 製品基点は空状態100件と同じBE/FE 93c8aa7。新規Profile・fixture・観測・集計のみを追加。
  別のマイク再認可修正3f8d53aを混在させていない。
- 各試行で専用の新規data root、会話履歴0件。固定fixture
  synthetic-greeting-preferences-v1（挨拶に関する架空のUSER_PREFERENCE 3件）の正本とindexを照合。
  形成・統合停止の実receiptを初期状態hashに含める。
- SQLite正本から既存workerが実embeddingでChroma派生indexを作成。
  実際の応答プロンプトに入った3件の記憶ID・版をresponse provenanceで確認し、匿名hashで保存。
- speech-v2、キャラクター・記憶ポリシーのhashは空状態の元manifestと一致。
  Ollama候補0.34.2／11534、CHAT gemma4:e4b、embedding nomic-embed-textを使用。
  model digestは実行後APIの観測値として保存し、全試行中のdigest採取と混同しない。
- TTS revision 0b97ed943aa660877dccc24b8df25cae6eec1549、
  image sha256:d8279a7a43adc105ebe14b4bd3620fe2839c44966ff756c8c93e5dde9fba916c。
  採用miori-b3-4221/B3 caption、seed4221、40steps、speed1.02、BF16、Graph256、参照cache、
  compile無効を保持。既存専用50026を使い、故障注入・共有サービス再設定は行わない。
- BE/FEの実行中image IDとresourceを全試行別に保存。固定source tagでもimage IDが試行ごとに異なる。
  1個のimageで25件を実行したとは扱わない。GPUの競合試験は併走しない。

## 判定と残件

合意した第2段階の測定作業と記録は完了。速度・参照結果に受入閾値を設けず、
2,000msや既存回帰式をこの値へ合格条件として適用しない。
記憶形成の影響・長い履歴の影響は範囲外。実マイク・聴感、再接続・相槌・休止、
更新／切り戻し、残レビューなどの全体受入は継続する。
GitHub Actionsは今回再開し、未統合PRを再実行中。全CI成功やEpic統合は別途確認する。
