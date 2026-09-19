# 候補起動中のOllama接続切断（2026-09-19）

候補BE/FEの59ee3f2396a4c312d3f7f1aaebfd5f4b8a8150b8を新しいimageとして起動すると、
Backendの全5用途のOllama startup probeがunavailableとなった。
同じ時間帯に、Ubuntuホストから専用Ollamaへ送った読み取り専用metadata probeでも接続失敗を観測した。
Backend内部だけのエラーではないことは確認したが、接続を切断する根本原因は未確定である。

| run | 結果 |
|---|---|
| 424-microphone-cancel-browser-0919-03 | Backend起動失敗、ブラウザ診断NOT_RUN。依存probe 312件中25件失敗（RemoteProtocolError 4、ReadError 10、ReadTimeout 11） |
| 424-microphone-cancel-browser-0919-04 | 同じimageで独立した手動再試行。マイク取消診断1/1成功、native SDK verified |

run 03のBackend起動は11:16:46.084 UTC、最初の外部probe失敗は11:16:47.916 UTC、
最後の失敗要求開始は11:17:03.058 UTCだった。
Frontend imageの生成は11:17:06.489 UTC、Frontend起動は11:17:06.930 UTC。
Frontend buildと切断期間が重なることは観測したが、buildによる障害と断定しない。
専用Ollamaコンテナは03:38:20.055 UTCから稼働し、restart count 0、OOMKilled falseだった。
対象時間のdogfood側Dockerイベントにコンテナ・networkの開始/停止はなかった。

現行Environment CLIはpre_probeの後、Backend build/start、Frontend build/startを直列実行する。
そのため、最初の依存確認に成功してもBackend起動中にFrontendをbuildし得る。
imageの準備とアプリ起動を分離する案は、次の検証候補であり本記録時点では未実装。

観測は0.5秒間隔で/api/tagsと/api/showへ直列要求し、各要求timeoutを1秒に固定した。
モデル生成は行わない。HTTP本文・例外本文を公開せず、時刻・状態コード・例外型だけを保存した。
観測負荷を加えた局所診断であり、正式TTFA・音質の測定ではない。
run 03の失敗をrun 04の成功で置換しない。ブラウザ入力は合成deviceで、実マイク受入は未実施。
両runの所有BE/FEの削除を確認し、共有サービスやdogfoodの設定・データは変更していない。

[匿名証跡](../artifacts/voice-startup-transient-20260919/evidence.json)、
[run 03の依存probe](../artifacts/voice-startup-transient-20260919/dependency-probe-03.json)、
[run 04の依存probe](../artifacts/voice-startup-transient-20260919/dependency-probe-04.json)、
[manifest](../artifacts/voice-startup-transient-20260919/manifest.json)を参照。
過去の[マイク取消診断](voice-microphone-cancel-20260919.md)の版・結果は変更しない。
