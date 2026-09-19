# 固定記憶参照の実接続診断（2026-09-19）

第1段階達成後、固定した合成記憶3件を検索・参照する第2段階の計測基盤を
[PR #454](https://github.com/FYuki/digital-souls/pull/454)へ追加した。
本書は比較cohort前の診断3runであり、20件や100件の性能分布ではない。
[全結果](../artifacts/voice-memory-reference-pilot-20260919/evidence.json)と
[原ファイルhash](../artifacts/voice-memory-reference-pilot-20260919/manifest.json)を保持する。

## 全run

| run末尾 | 版 | 結果 |
|---|---|---|
| 01 | d015c1a | Backend準備timeout。音声試行NOT_RUN。終了後にcontainerが回収され、起動logを取得できず根本原因は未確定 |
| 02 | d015c1a | 記憶3件の参照・音声再生成功。ただしSDK/resource観測のProfile許可漏れで全体終了1 |
| 03 | 4e41ac3 | 観測修正後、実参照・実再生・native SDK照合・後始末まで成功、終了0 |

IDは350-memory-reference-pilot-0919-XX。既存rootや失敗記録を上書きせず、
自動retryなしで別runとして実施した。02の音声成功をSDK検証済みに読み替えない。
許可漏れは2件の回帰テストで再現し、修正後28件が成功した。

## 確認範囲

最新03は架空の挨拶嗜好3件が応答プロンプトへ採用されたことを、
正本の固定ID・content versionとmemory_response_dependenciesで照合した。
空履歴、形成・統合停止receipt、実embeddingによるChroma index、固定内容・設定hashを確認。
参照件数を設定だけから推定していない。

03の発話終了→実ブラウザ再生開始の保守側値は2292.80ms。
これは1試行であり、p50/p95でも2秒の合否でもない。第2段階に合格閾値は置かない。
sample数一致・gap 0、Session終了、実native SDKを確認。
全3runの所有Backend/Frontend teardown完了を照合した。

BE/FEの製品基点は第1段階と同じ93c8aa7。今回追加したのは計測分岐・fixture・観測許可。
TTSは0b97ed9／image d8279a7a43adの既存専用候補を使用し、故障注入を行っていない。
声・40steps・seed4221・speed1.02・BF16・Graph256・参照cacheを維持。
記憶形成、長い会話履歴、人の実マイク・聴感を測った証拠ではない。

## 継続する比較

093b423で準備5回＋測定20独立試行を事前登録した
350-memory-reference-full-0919-01は完了し、[全結果](voice-memory-reference-20260919.md)へ分離した。
実行基盤と全実測証跡は別PRに分け、成功・失敗・未参照・欠測を全分母に保持する。
