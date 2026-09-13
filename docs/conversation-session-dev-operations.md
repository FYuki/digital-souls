# dev音声会話の連続操作試験

起動済みdevに対し、Windows ChromeとPlaywrightで入力欄のフォーカス切替、マイクのオン／オフ、再生中のテキスト送信を繰り返す。最後の短い回答は出力完了まで待つ。HTTPのreadyや再生開始だけを合格にしない。

## 実行

`frontend`を作業ディレクトリにし、ChromeとPlaywrightが利用できる環境で実行する。

```sh
node scripts/verify-dev-voice-operations.cjs voice-operations.json
```

既定はUI `http://localhost:5173`、API `http://localhost:8000`、8回の割り込み。
必要に応じて `DEV_URL`、`DEV_API_URL`、`VOICE_OPERATION_CYCLES`（1〜32）を指定する。
別のNode環境にPlaywrightを導入済みの場合は `PLAYWRIGHT_MODULE_PATH` でそのモジュールを指定できる。
`HEADED=1`でChromeの操作を表示できる。利用者の通常プロファイルは使用しない。

試験は光織の新規スレッドを1件作る。終了時には試験が作った音声sessionだけを終了し、スレッド履歴は検証用に残す。
結果JSONはイベント種別・識別子・再生完了・障害分類を記録し、音声本文やtokenは保存しない。
失敗または時間切れの場合は終了コード1を返す。

実LiveKit/Ollama/VOICEVOXを通すがマイクは模擬デバイスである。実マイク、スピーカーの回り込み、音質のユーザー受入は別途必要。

## 2026-09-12 の軽減策

変更前は4〜7回の送信で `event deduplication capacity exceeded` を再現した。
受信履歴256件を会話全体に適用しており、細分化された回答deltaで枠を消費していた。
backend側の入力履歴も256件、出力履歴も512件だった。

`ea77807`で前後の重複検知履歴を16,384件・16MiBへ拡張した。
送信待ちoutboxは256件・1MiBのままである。同じIDの内容改変、再送の重複、sequence欠落、明示的な容量制限は引き続き検証する。

これは有限の履歴枠を拡張する軽減策であり、無期限会話の保証ではない。
長時間の連続利用で上限へ到達する可能性は残る。履歴の安全な廃棄には再送・再接続保証と合わせた別の設計が必要となる。
フロント862件、backend関連130件、型検査、ビルドが成功した。

更新後のdevでは8回の送信、9応答の再生開始、最後の応答の再生完了、接続失敗0件を確認した。受信イベントは489件で、旧上限256件を超えている。結果集計は `docs/artifacts/conversation-session-dev-operations-2026-09-12.json`。
