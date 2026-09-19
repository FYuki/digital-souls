# 高速化TTS候補の再接続診断（2026-09-19）

[PR #455](https://github.com/FYuki/digital-souls/pull/455)で既存cohortを専用推論候補へ接続した。
本書は正式100件に先行する診断で、[全記録](../artifacts/voice-reconnect-pilot-20260919/evidence.json)、
[匿名集計](../artifacts/voice-reconnect-pilot-20260919/report.json)、
[原記録と公開ファイルhash](../artifacts/voice-reconnect-pilot-20260919/manifest.json)を保持する。

## 失敗を含む全計画

| cohort | 予定 | 実施 | 結果 |
|---|---:|---:|---|
| 424-candidate-reconnect-pilot-0919-01 | 3 | 準備1、音声0 | Backend起動probeで全Ollama targetがunavailable。readiness期限で停止、残り2件未実施 |
| 424-candidate-reconnect-pilot-0919-02 | 3 | 音声3 | 3/3で復旧・同一Sessionの後続音声・終了・所有container削除を確認 |

初回のroot・logを上書きせず、手動の調査後に別IDで実施した。
初回の原因は未確定。事後のホスト／同じBackend image／次の専用Backendからはモデル情報APIが200で応答した。
一時的な準備失敗を解消済みと断定しない。初回を正式cohortのwarm-upや成功へ読み替えない。
初回の終了コード2は準備・観測未成立、02の終了コード1は正式受入に必要な100件未満によるもの。

## 復旧の実測と範囲

固定版c0f8a5a、LiveKit専用bridgeだけを2秒切断。
リンク復旧の時計範囲上限から、新しいcontrol往復と新しい有音RTC probeの実出力までを測る。
診断3件の両経路復旧p50は2441.34ms、p95は2441.53ms。10秒以内3/3、
後続通常音声3/3、全件gap 0、sample数保存、Session終了、fault時計process終了、実native SDKを確認した。

重複0件の観測範囲は同じresponse・SSRC・RTP timestampのsample出力区間重複である。
異なるSSRCにまたがる同一PCM内容の重複を独立に証明したものではない。
probe復旧時間を通常発話のTTFAやLLM/TTS完了時間へ置き換えない。
復旧後の同一Sessionで別responseの通常STT→LLM→TTS→実再生を別途確認した。

## 固定条件と所有

BE/FEはc0f8a5aで、マイク再認証修正3f8d53aを含む。
Ollama 0.34.2の専用11534、TTS0b97ed9の専用50026を使用。
Irodori採用声miori-b3-4221、seed4221、40steps、speed1.02、BF16、
compile無効、CUDA Graph上限256MiB、参照latent cache有効を維持する。

LiveKitは既存の停止中test専用containerについて、purpose/test label、設定hash、
loopbackの19880/19881/19882、専用bridgeの排他性を確認して起動した。
通常dev・dogfoodのLiveKitと共有推論の設定・配備を変更していない。
各試行は独立data root、RAG無効、記憶形成・統合停止のreceiptを保存する。

## 正式測定

同じ固定版で424-candidate-reconnect-full-0919-01の100独立Sessionを事前登録し実行中。
4件目には約3940.90msの復旧があり、個別の3秒判定は未達だが10秒以内の復旧と後続発話は成功した。
失敗を含め全予定試行を残し、最終p95・成功率・coverageを確定してから正式判定する。
診断3件の成功で100件の受入、実マイク・聴感、相槌、文中休止、更新／切り戻しを完了にしない。

## 再接続待機の切り分け

[時刻対応の診断](../artifacts/voice-reconnect-pilot-20260919/delay-diagnostic.json)は、
実行中の正式cohortから通常例1件と最初の3秒超過2件だけを抽出した原因調査である。
抽出例の数値を正式p50/p95や成功率へ代用しない。

固定依存livekit-client 2.22.1の実装には、peerの再接続確認前に必ず2000ms待機する処理がある。
試行1/4/10のブラウザ送信観測では、SDKのreconnected通知直後に待機中の制御送信がまとまって再開した。
4件目はその後にも追加の配送待ちとgeneration 1から2への交換があり、固定待機だけで全遅延を説明しない。

時計は全てBrowser monotonicへ対応付け、復旧操作開始の下限から保守側の差を保存した。
Playwright側のsocket時刻を直接減算していない。
この調査はSDK待機の削除・短縮を安全と判定したものではない。
ICE再交渉前の古いconnected状態を復旧済みと誤認しない条件、切断・取消・再失敗時の挙動確認が必要である。
正式測定中のコード・SDK・閾値は変更しない。
