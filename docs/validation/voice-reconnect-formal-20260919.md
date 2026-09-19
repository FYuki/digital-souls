# 高速化TTS候補の再接続100件（2026-09-19）

[先行診断](voice-reconnect-pilot-20260919.md)と同じ固定版で、
424-candidate-reconnect-full-0919-01の100独立Sessionを事前登録して測定した。
[全試行・条件・原記録hash](../artifacts/voice-reconnect-formal-20260919/evidence.json)、
[既存reporterの匿名集計](../artifacts/voice-reconnect-formal-20260919/report.json)、
[公開ファイル・計測コードhash](../artifacts/voice-reconnect-formal-20260919/manifest.json)を保存する。

## 結果

| 項目 | 観測 |
|---|---:|
| 予定／記録 | 100 / 100 |
| 独立Session／会話 | 100 / 100 |
| 検証済み障害注入／完全な出力証拠 | 100 / 100 |
| 疎通回復後10秒以内 | 100 / 100 |
| 両経路復旧p50／p95 | 2447.81 / 2696.97 ms |
| 同一Sessionの後続通常音声成功 | 100 / 100 |
| Session終了／fault時計process終了 | 100 / 100 |
| 観測対象のsample区間重複 | 0 |
| 個別3秒超過 | 3 |

既存reporterの正式再接続判定は **PASS**。
全coverage=true、
10秒以内率=true、
p95 3,000ms以下=true。
runnerの各試行の3秒判定と、全100件の正式p95判定を混同しない。
超過した試行は除外せず原記録・匿名全件表に残す。

## 条件と測定境界

BE/FE計測版はc0f8a5a0955ea021c84ffcbef0e4be82fe576cde。
統合PR #455の最終head 29cf1c8は別commitであり、100件をその版の再実行とは扱わない。
専用Ollama 0.34.2、TTS0b97ed9の固定image、miori-b3-4221、
seed4221、40 steps、speed1.02、BF16、compile無効、
CUDA Graph上限256MiBと参照latent cacheを維持した。
各試行の起動image・実native SDK・形成/統合停止receipt・独立data root・所有アプリ回収を照合した。
後続音声は全100件でrenderedSamplesとexpectedSamplesが一致し、gapSamplesは0だった。

専用test LiveKitのbridgeだけを2秒切断した。時計校正の範囲を保ち、
リンク復旧後の新しいcontrol往復と新しい有音RTC probeの実出力までを測る。
その後、同じSessionで別responseの通常STT→LLM→TTS→ブラウザ実再生を確認した。
probe復旧時間は通常発話のTTFAではない。

重複計測の範囲は同じresponse・SSRC・RTP timestampのsample出力区間重複である。
異なるSSRCにまたがる同一PCM内容の重複を独立に証明したものではない。
過去の移設前後比較とはモデル・TTS構成が異なるため、この100件だけで#358の前後回帰式の成立を主張しない。

測定序盤には同ホストで統合worktreeのPython259件・FE35件・型検査を実行した。
追加GPU推論は行わず、その後のローカルテストを保留した。
原記録の軽い読取とGit/GitHub操作は並行しており、完全なホスト無負荷試験とはしない。
各試行のresource観測と全外れ値を保持する。

## 残条件

これは固定WAV・専用候補構成での再接続測定である。
人の実マイク・聴感、相槌・割り込み・休止の全受入、
FE/BE一括更新・切り戻し、残レビューを完了にしない。
測定終了後、所有ID・用途・test labelを照合して専用障害LiveKitだけを元の停止状態へ戻した。
通常dev LiveKitの稼働は維持した。共有サービスへの配備とmainマージは行っていない。
