# Irodori検証の一次証跡

公開集計は本文・識別子を含まない固定schemaを維持する。実行ログの場所は、集計と同じ名前の.provenance.jsonで管理し、集計本体の数値・成功数・既存hashは変更しない。

| 集計 | 対応する保存先・一次証跡hash |
|---|---|
| [正式割込100試行](take-turn-100-report.json) | [take-turn-100-report.provenance.json](take-turn-100-report.provenance.json) |
| [正式旧応答停止監査](stale-output-100-report.json) | [stale-output-100-report.provenance.json](stale-output-100-report.provenance.json) |

2レポートの元runはirodori-329-take-turn-100-01、計測revisionはdbc620e3c1eb4b0b44067321cbee96b2cb3ced23。保存先はWSL Ubuntu内の /home/asa/dev/digital-souls-evidence/irodori-329/irodori-329-take-turn-100-01。元runディレクトリを残したまま、下記5ファイルをコピーし、コピー前後のSHA256一致を確認した。公開ダウンロード用URLではなく、実行ホスト上の保存先である。

- trial-manifest.json: 100試行の実行manifestと再生停止の観測証跡。
- runtime-data/voice-metrics/controlled-trace.jsonl: 対応する制御・応答の実行trace。
- playwright-results.json: Playwright実行結果。
- inference-runtime.jsonl: 当該固定試行の推論runtimeログ。
- native-sdk.json: 使用したnative SDKの記録。

各出典ファイルは、対象集計自身のSHA256、保存先、上記一次証跡の相対パス・サイズ・SHA256を記録する。manifest/traceのhashは集計中の既存hashとも照合済み。生ログはローカル所有ユーザーに限定し、本文や識別子を含み得る一次証跡を公開集計へ混ぜない。

再検証時はWSL Ubuntuで出典JSONのstorage.pathとprimary_evidence[].relative_pathを組み合わせ、SHA256を照合する。保存先を移動する場合は出典ファイルを更新し、hashを再確認する。
