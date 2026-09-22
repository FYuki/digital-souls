# 会話終了後の遅着マイクtrack診断（2026-09-19）

[製品修正#458](https://github.com/FYuki/digital-souls/pull/458)を含む固定版で、
実ブラウザ・実Backend・実SFUを使う局所診断が1/1成功した。
ブラウザの合成入力deviceによる検証で、人の実マイク・native許可ダイアログの受入ではない。

製品commitは256c52007932a161811b1b46d1357f97fce45366、
[診断基盤#464](https://github.com/FYuki/digital-souls/pull/464)は5804c6905202827a59bcfdd67cc24f78b962cbdd。
型検査はエラー・警告0、収集は意図した1件のみ。
専用integration-irodori-cuda-graph、Ollama 0.34.2、TTS 0b97、形成/統合停止を使用した。

実getUserMediaのtrack取得後、Promiseをアプリへ返す直前だけを保留した。
UIで会話を終了し、実終了APIの200/endedを確認してから取得済みstreamを渡した。
trackはliveからendedになり、マイクOFF・入力停止・応答開始0件、終了API1回を確認した。
本測定に通常会話の応答再生や性能100件を含めない。

| run | 結果 |
|---|---|
| 424-microphone-cancel-browser-0919-01 | BackendのOllama起動probeが全用途unavailable。テストNOT_RUN |
| 424-microphone-cancel-browser-0919-02 | 同じ固定版でテスト1/1成功。native SDK verified |

初回の失敗と次runの成功を別々に保持する。間欠的な起動probe失敗の根本原因は未解決。
両runとも所有BE/FEのteardownとコンテナ消滅を確認した。
共有サービスの設定・再起動・dogfoodデータの操作を行っていない。

[匿名証跡](../artifacts/voice-microphone-cancel-20260919/evidence.json)には
実image ID、ブラウザの検証値、元ログ・Profile・native SDK・Playwright結果のhashを保存した。
[manifest](../artifacts/voice-microphone-cancel-20260919/manifest.json)で公開JSONのhashを確認できる。
音声本文、会話ID、認証token、入力PCMを公開していない。
