# image準備をアプリ起動前へ分離した実起動確認（2026-09-19）

[製品修正#467](https://github.com/FYuki/digital-souls/pull/467)の固定版
3bb9a0917dc4764d026c94078902fbd14ff01f5dを初めてbuildし、両image準備後の依存確認から
実ブラウザのマイク取消診断まで1/1成功した。native SDKを確認し、所有BE/FEを回収した。

runは350-image-preparation-browser-0919-01。
読み取り専用のOllama metadata probeは138件中32件失敗したが、すべてBackend起動前だった。
最後の失敗要求終了は11:34:30.665 UTC、Backend開始は11:34:33.862 UTC、
Frontend開始は11:34:34.119 UTC。Backend起動後の観測失敗は0件。
BE/FEのimage createはそれぞれ11:34:13.513 / 11:34:33.335 UTCで、
両imageを揃えてからBackendを起動した順序をDockerイベントでも確認した。

初回起動の処理順を変えることで、今回の一時切断中にBackendのstartup probeを実行する状況を避けた。
一方、Environment CLI開始前の11:33:58.769 UTCからも接続失敗があり、
一時切断の原因すべてをFrontend buildとは断定できない。切断自体の根本原因解決や、
すべての起動条件での成功を、この1件から保証しない。
失敗を隠す自動再試行や、固定待機時間の追加は行っていない。

前の[固定59ee3f2の初回失敗と手動再試行](voice-startup-transient-20260919.md)は保持する。
今回は自動入力deviceを使用した開始取消診断であり、人の実マイク・聴感や正式速度の受入ではない。
変更はimage準備とアプリ起動の順序に限定され、準備済み会話のSTT/LLM/TTS処理は変更していない。
そのため正式TTFA 100件は繰り返さず、起動と関連回帰を確認した。
空状態p95 2秒の目標と、速度へ影響する変更に再計測する方針は維持する。

[匿名証跡](../artifacts/voice-startup-image-preparation-20260919/evidence.json)、
[依存probe](../artifacts/voice-startup-image-preparation-20260919/dependency-probe.json)、
[Docker時刻](../artifacts/voice-startup-image-preparation-20260919/lifecycle.json)、
[manifest](../artifacts/voice-startup-image-preparation-20260919/manifest.json)を参照。
共有サービス・設定・dogfoodデータは変更していない。


## probeの原記録と公開JSONの対応（最終レビュー補足）

evidence.jsonのraw_sha256.probeは、収集した元JSONLを照合する。
公開dependency-probe.jsonは138行を同じ順序のrows配列へ格納した整形JSONであり、バイト列のhashは異なる。
元JSONLのhashと既存raw_sha256.probeの一致、および全138行の値・順序の一致を再確認した。
[変換対応](../artifacts/voice-startup-image-preparation-20260919/probe-provenance.json)に元hash・公開hash・形式を記録した。
manifestは引き続き公開ファイルのhashを持つ。元hashを公開版のhashへ置き換えていない。
