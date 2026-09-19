# 実アプリの更新・切り戻し確認（2026-09-19）

## 結果

変更前main e51bce16 → 候補f5e8228b → 変更前main e51bce16を、同じ専用データで往復した。
各段階で実FE/BE・Whisper・LLM・TTS・LiveKitを使用し、固定音声の実再生完了と履歴追記を確認した。
バックアップへの復元や履歴の削除は行っていない。実マイクと速度の評価は別証跡である。

| 段階 | 全保存行数 | 対象会話の行数 | 直前の全保存行 | 実音声往復 |
|---|---:|---:|---|---|
| main側の事前確認 | 6 | 5 | 既存5件の全列ハッシュが一致 | 完了 |
| 候補へ更新 | 7 | 6 | 6件の全列ハッシュが一致 | 完了 |
| mainへ切り戻し | 8 | 7 | 7件の全列ハッシュが一致 | 完了 |

完了・privacy_skipped・interruptedを含む。保存拒否のユーザー本文と応答本文はDBでもNULL、
APIでも本文キーを返さない。実再生のprefix 0がACKされた後に会話終了し、
完了前の応答がinterrupted・再生済み9文字として保存された。その行も各版で維持された。
終了APIは各成功試行で200。ネイティブLiveKit SDKの実ロードは各版でverifiedだった。

新版APIへ旧protocol 1.1を送信すると、409 / protocol_version_mismatchが返った。
実接続で確認したのは拒否応答であり、資源を作らないことは既存unit/moduleの根拠と分ける。
中断直後のactiveAudioGraphsは最終観測値1のため、これを即時無音の追加証明には使用しない。

## 保持した失敗と適用範囲

最初に切り戻し先へ選んだ07b8b1ee（2026-09-14、Irodori接続の版）は、
意味記憶のschema 6導入より古く、候補で更新したpersona-memory.dbを拒否した。
起動確認は2回失敗し、FAILを保持する。schema.pyのblobは、07b8b1eeで
466b1b5c8b36f483f1c7d2248a626bbdcc8d58a9、現在mainと候補では共通の
16319e00daa2daa1a61dd7516a60f1679308596fである。差分はmainへ既に統合された#405に由来する。
今回の変更前という基準を満たすmain e51bce16へ選び直し、改めて往復確認した。
07b8へデータを維持して戻せるという保証へ広げない。

試験ハーネスでも、履歴kindの誤判定、一次エラーを覆うfinally、再読込後の未選択、
起動不成功中の開始があった。全失敗を保持し、サイドバーの実操作を使うよう修正した。
最初の失敗時に既に保存された1件も残している。このため全保存行数は対象会話より1件多い。
実マイクのクリック音についてはユーザーが位置調整で解消・追加対応不要としたため、
この試験で追加対策や認識設定変更は行っていない。

## 再現情報・所有範囲

[匿名化した結果](../artifacts/voice-application-rollback-20260919/result.json)に固定版、
各段階の件数・状態・保持照合結果、native SDK状態、失敗、終了結果を記録した。
生の会話・トークン・native payload・DB由来のハッシュ・会話ID・container IDは掲載しない。全列ハッシュの比較と所有containerの照合はローカルで行い、詳細は専用試験ディレクトリに保持する。[manifest](../artifacts/voice-application-rollback-20260919/manifest.json)で保存物を照合できる。

専用Environment ID dev、FE 5173、BE 8000、ready gate 4174を使用した。
実マイク候補のtest環境18573は維持し、終了後もFE 200 / ready gate 204を確認した。
共有サービスの再起動・設定変更は行わず、標準Environment CLIで専用dev環境の所有資源だけを停止した。

同梱の試験ソースは実行時の写しである。旧版frontendの
test-results/practical-rollback/<run>/harnessへ配置すると相対importが元の位置に対応する。
各固定版をEnvironment CLIで起動してreadyを確認してからPlaywrightを実行する。
ROLLBACK_ROOT、ROLLBACK_PHASE、ROLLBACK_RESULT_DIR、ROLLBACK_REPORT、ROLLBACK_ARTIFACTSを
試行ごとの専用ディレクトリへ指定する。中断試験はROLLBACK_TEST=interruption.spec.tsを指定する。
history-state.jsonは私的な会話IDとハッシュを持つため公開しない。
