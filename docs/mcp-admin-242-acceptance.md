# 外部MCP接続管理の受入（#242）

## 対象

#280の接続・専有credential保存、#281の接続確認/実行状態、#282の管理UI、#283の実接続受入を対象とする。
現在の操作・保存契約は[管理運用手順](addon-admin.md)を参照する。

## 確認した動作と証跡

| 境界 | 確認内容 | 証跡 |
|---|---|---|
| 保存 | 接続ごとのcredential専有、同じ値の重複保存、再起動復元、secret_refの非入力 | `backend/tests/unit/test_mcp_connection_store.py` |
| 削除 | 接続/credentialの同一transaction削除、失敗注入で双方rollback | 同上の削除失敗テスト |
| 履歴 | 設定・credential更新でrevisionを進め成功履歴をリセット、表示名のみ維持、古い結果拒否 | 同上と`test_mcp_connection_management.py` |
| runtime | OFF登録、初回確認失敗でも保存、確認成功後のみON、ON編集失敗時の希望状態維持と復旧 | `backend/tests/unit/test_mcp_connection_management.py` |
| 競合 | 確認待ちONに対する後着OFF、編集中の古い確認、Tool実行中の変更/削除拒否 | 同上 |
| 実行中の実接続 | 別processがToolを受信した印を確認してから変更・credential更新・削除を拒否。表示名だけ変更し、復旧後は古いloopを拒否 | `backend/tests/integration/test_mcp_admin_transport_integration.py` |
| 新旧MCP | 新protocolはserver/discover、旧protocolはpingでhealth確認。HTTP/stdio、認証、取消しの既存境界 | `backend/tests/module/test_external_mcp_conformance.py`、`backend/tests/unit/test_external_mcp_client.py` |
| UI | credential専用送信/入力消去、typed stdio、削除確認/実行中拒否、Tool説明のテキスト表示 | `frontend/src/lib/ExternalConnectionEditor.module.test.ts` |
| ブラウザ | 実Core/Vite/HTTP・stdio MCPによる登録→ON→編集→再起動→接続断→削除。0 Tool、Bearer重複・更新、320px表示 | [公開受入結果](artifacts/mcp-admin-242/browser-public.json)、[固定イベントログ](artifacts/mcp-admin-242/browser-execution.jsonl) |
| secret非露出 | 不正payloadの固定エラー、通常responseへの非露出、秘密値の更新後もredaction対象を維持 | 保存/管理APIテスト、ブラウザ受入のprocess/browser log検査 |

## 再実行

```bash
backend/.venv/bin/python -m pytest backend/tests/unit/test_mcp_connection_store.py backend/tests/unit/test_mcp_connection_management.py backend/tests/unit/test_external_mcp_client.py
backend/.venv/bin/python -m pytest backend/tests/module/test_external_mcp_conformance.py backend/tests/module/test_addon_admin_api.py
RUN_MCP_ADMIN_REAL_TESTS=true backend/.venv/bin/python -m pytest -s backend/tests/integration/test_mcp_admin_transport_integration.py
backend/.venv/bin/python scripts/acceptance_mcp_admin.py
```

ブラウザ受入はPlaywrightのChromium、Ollama、既存の`gemma4:e4b`と`nomic-embed-text:latest`を必要とする。
Ollamaは一時ポートのテスト所有processで、Core起動時のモデル存在確認だけに使う。推論生成は行わない。
モデルの保存先は`MCP_ACCEPTANCE_OLLAMA_MODELS`で指定可能。既存モデルの再取得や環境の再構築は行わない。
日本語画面を画像でも確認する場合は、日本語フォントを参照する`FONTCONFIG_FILE`を指定する。

すべてdev/test専用の一時data rootと動的portを使用し、dogfoodのサービス・履歴・credentialへ接続しない。
起動したサービスは終了処理で停止し、成功した全段階とDB削除・log非露出・teardownを確認した後だけ公開結果を書く。
再実行の開始時に前回の成功証跡を削除し、途中失敗を成功と混同しない。

## 証跡の適用範囲

ブラウザ受入ではページ通信やMCP通信をモックへ置換していない。一方、MCP接続先はこのrepoの別process制御fixtureであり、
第三者MCPサービス、外部発行の実credential、実LLMによるTool選択や実会話の受入を意味しない。
Tool Routingは未設定であり、接続管理が会話LLMから独立して動作する範囲を確認する。
実行中・race・rollbackの失敗注入は各テストの検証範囲として区別し、ブラウザだけで検証したとは扱わない。

公開結果は固定のcheck名だけを保存する。process logと画面画像はテスト専用の一時診断領域へ置き、
実credentialや外部本文をIssue・PR・成果物へ記載しない。
