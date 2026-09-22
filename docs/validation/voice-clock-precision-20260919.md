# 音声速度集計の時計精度補正（2026-09-19）

CodeRabbit #463の4053260557を検証すると、reporterが整数msへ丸められたfirst_playback traceを集計し、
実出力時計の小数精度を持つmanifestのstartedAtから直接算出した公開evidenceと差があった。
空状態の基準側にも同じ差があるため、片側だけを補正せず両cohortの元manifest・traceを全件再検証した。

| TTFA | 元report | 精度補正後 |
|---|---:|---:|
| 空状態100件 p50 | 1796.15ms | 1796.50ms |
| 空状態100件 p95 | 1967.65ms | 1968.00ms |
| 記憶参照20件 p50 | 2190.25ms | 2190.43ms |
| 記憶参照20件 p95 | 2338.62ms | 2338.78ms |
| 記憶参照と空状態のp50差 | +394.10ms | +393.93ms |
| 記憶参照と空状態のp95差 | +370.97ms | +370.79ms |

発話終了の因果下限、同じbrowser時計、Hyndman–Fan type 7、全予定分母を維持した。
追加音声試行は0件。補正コードは#473、f3fd6a09であり、製品の速度変更ではない。
空状態の100件、記憶参照の20件が再検証でき、失敗・欠測・除外は0件。
空状態p95は引き続き2000ms以下。記憶参照には受入閾値を設けない。
元report・evidence・manifestは上書きしない。本補足がTTFAの現行集計値であり、原記録は当時の出力として読む。

[補正結果](../artifacts/voice-clock-precision-20260919/result.json)は元公開reportのhash、分母、補正前後の数値を持つ。
会話ID、container ID、DB由来hash、会話本文は追加公開していない。
再現には既存cohortのtrial-manifest.jsonとcontrolled-trace.jsonを使い、修正した
app.livekit_pilot_report.finalize_livekit_controlledで空状態を別出力へ再集計し、その結果をbaselineとして
report_memory_reference.summarizeへ渡す。元ファイルを書き換えず、新しい出力先を使う。

## 参照cacheの過去比較に関する補足

#463の4053260570と4053260573も確認した。過去のtuple比較はzipの共通prefixだけであり、
出力長の一致を検査していなかった。過去の完全一致フラグを、長さを含む全出力一致の単独証明として扱わない。
現在のプローブは長さとstrict zipを検査する。

過去の3反復×5文は反復単位で順序を変えており、baseline先行10組・candidate先行5組だった。
熱・cache・GPU clock等の時間変化の偏りを排除できない。観測中央値やraw trialは保持し、
一般的な改善率や因果効果の保証へ広げない。
現在のプローブは通し番号で交替し、15組なら先行8対7にする。奇数組なので完全同数とはしない。
各組の順序も保存する。過去のprototypeソースは実行時の写しとして変更しない。
新しい均衡化プローブでのGPU再計測はNOT_RUNであり、過去の結果を新実行の証拠に置き換えない。
