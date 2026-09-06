# #182 受入証跡（2026-09-07）

公開MCPと制御用MCPの結果を分けて記録する。再実行入口は[会話からの外部MCP利用](../../tool-use.md)。

| 検証 | 一次証跡 | 範囲 |
|---|---|---|
| 公開Filesystem／Everything | `published-mcp-browser.json` | ブラウザのテキストでstdio ToolとHTTP Resourceを参照。音声で各参照から回答・AudioTrack再生までを確認 |
| 制御用MCP | `controlled-mcp-browser.json`、`controlled-mcp-browser.txt`、`controlled-runtime.json` | テキストのMRTR質問・回答・再開、実行中の停止、音声の質問再生中に回答してbarge-in・再開・最終回答再生 |
| Backend単体 | `backend-unit.txt` | 基準の全体実行。後続のTool変更は集中回帰とPRの最新CIで確認 |
| Backend結合 | `backend-module.txt` | 別process MCP conformance、会話・履歴・環境等の全体実行 |
| MCP／Tool集中回帰 | `tool-gate.txt` | 元schema、snapshot、bindingの呼出し固定と失効、MRTR、停止、秘密情報、予算・省略、完了判定 |
| Frontend単体・結合 | `frontend-unit-module.txt` | 外部参照表示、停止、ページ終了、既存会話機能 |
| 通常画面のmock E2E | `browser-mocked.txt` | 既存の画面・音声状態遷移。実サービスの代用にはしない |
| 型・lint・build | `check.txt`、`lint.txt`、`build.txt` | 静的検証とFrontend build |

実接続はOllama `gemma4:e4b`、Whisper、VOICEVOX、テスト所有LiveKitを使用する。
公開MCP試験の入力はChromiumのファイルマイク、制御MCP試験はWebAudioのMediaStreamに流す合成発話である。
STT、LLM、TTS、MCP通信、LiveKit mediaと再生を置換していない。物理マイク・人の発話品質の証跡ではない。

Backendの単体・結合を同一processで連続実行した際には4件が失敗したため、個別再現と層別実行を行った。
個別4件および単体・結合の分離実行は成功した。失敗をskipへ変換していない。
PRのCIは常に最新commitの結果を参照し、CodeRabbitのskip statusを実レビュー完了と扱わない。
