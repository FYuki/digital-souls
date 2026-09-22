# 最新mainとの通知統合検証（2026-09-22）

main `64a9d60c4d03eff73ec1f9708852fbb111bb5628`を通知Epicへ統合した`ca91d287c57ddf04834a3dcddd47a8e877a821d6`で検証した。テキスト競合はCIのBackend実行上限のみで、mainの25分を採用した。最新の音声開始準備・測定用記憶設定を維持している。

- Backend関連試験：49件成功（通知保存・参照・通知専用起動・通常起動・音声測定設定）。
- Backend型検査：375ファイル成功。
- Frontend：通知4件成功、型検査エラー／警告0、production build成功（既存のbundleサイズ警告あり）。
- 本番appと独立MCP fixtureによる実ブラウザ検証：6項目成功。個別通知、画面終了中の保存・件数上限、明示的な内容取得と既読、OFF／ON、絞り込み、期限切れ結果を確認した。
- [検証結果](browser-conformance.json)、[PC画面](desktop.png)、[モバイル画面](mobile.png)。ソースdigestは結果JSONに記録した。

実行には専用の一時データ・空きポートを使用した。LLMなしの通知専用起動を確認したもので、実外部サービス・dogfoodの受入ではない。過去の2026-09-16の記録は変更していない。会話側の独立参照保持・要約・報告は#364／#365／#366の後続範囲。

再実行：
```bash
python scripts/acceptance_notifications.py --output docs/artifacts/notification-183/main-sync-2026-09-22
```
