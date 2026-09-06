# 外部MCP実接続受入（#104）

## 対象と結果

2026-09-06、Ubuntu-devで実施。公開済みの独立したTypeScript実装へ、productionの
`ExternalMCPClient` / `Registry` / `ExecutionGate`から接続した。
リポジトリ内の合成MCP fixtureで外部serverを置換していない。

| 対象 | 固定version | 検証 |
|---|---|---|
| `@modelcontextprotocol/server-filesystem` | `2026.8.31` | stdio接続、14 Tool discovery、許可directory内の読取、範囲外の拒否、終了 |
| `@modelcontextprotocol/server-everything` | `2026.8.31` | Streamable HTTP、13 Tool / 7 Resource / 4 Prompt discovery、echo、Resource read、切断 |
| nginx → Everything | `nginx:1.28.0-alpine`のdigest固定 | Bearer成功・不正token拒否・未認証拒否 |

両MCP Serverとは`2025-11-25`へnegotiateした。clientは`mcp==2.0.0`、
Python 3.12.3、Node.js v24.20.0。
公開packageと推移依存は`infra/testing/mcp-real-servers/package-lock.json`で固定する。
nginxのdigestは`sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284`。
参照元は[公式reference servers](https://github.com/modelcontextprotocol/servers)。

実接続5件と関連unit/module 68件が成功した。
一次証跡は[`test-evidence/mcp-104/real-and-regression.log`](test-evidence/mcp-104/real-and-regression.log)。
対象ファイルは以下。

- `backend/tests/integration/test_external_mcp_real_servers_integration.py`
- `backend/tests/unit/test_addon_contracts.py`
- `backend/tests/unit/test_external_mcp_client.py`
- `backend/tests/unit/test_external_mcp_registry.py`
- `backend/tests/unit/test_external_mcp_gate.py`
- `backend/tests/module/test_external_mcp_conformance.py`
- `backend/tests/module/test_test_suite_classification.py`

## 発見した不具合と修正

HTTP server停止後のTool callで、SDKのbackground task失敗が接続を所有するCore taskへ
`CancelledError`として伝播した。正常系の実接続は通っていたが、切断の受入は失敗した。

SDKのcontext managerを専用taskでenter/exitし、request側は接続taskの終了も監視する。
HTTP transport失敗の原因をSDKのdispatcher終了前に記録し、共通envelopeのtransport失敗へ変換する。
HTTP応答やMCP応答は合成しない。呼出側からのcancelは引き続き伝播する。

切断でCore taskがcancelされないことと、呼出側cancel後にstdio processが終了することを
通常CIのmodule回帰テストへ追加した。元の合成MCP suiteも通過している。

## 再実行

Linuxの同一hostにNode.js、Backend依存、Docker Engineを用意する。
Dockerのhost networkからテストprocessのlocalhostへ到達できることが必要。
リポジトリルートで次を実行する。

```bash
npm ci --prefix infra/testing/mcp-real-servers --ignore-scripts
docker pull nginx@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284
RUN_MCP_REAL_SERVICE_TESTS=true \
MCP_REAL_SERVER_ROOT="$PWD/infra/testing/mcp-real-servers" \
npm run test:integration:mcp
```

明示開始前はopt-in未設定でskipする。明示開始後の依存不足、起動失敗、認証失敗、接続失敗は
skipへ変更しない。npm installやimage取得はテスト本体から実行しない。

## 環境分離と検証の限界

- 一時directory内の合成ファイルだけを読む。実ユーザーデータ、dogfood、共有推論serviceを使わない。
- HTTPは動的空きportを使用する。nginxはlocalhostにbindする。Everythingの公開版listenerは全interfaceにbindするため、隔離されたdev/test hostで実施する。
- HTTP serverへ渡す環境はPATH、テストHOME、PORTのみ。stdioはSDKの最小環境継承を使用する。
- nginxのBearer値はrunごとに生成し、一時configはcleanupで削除する。通常logと証跡にtokenを保存しない。
- 自分が作成したprocessとnginx containerだけを終了する。既存dev/dogfoodをstart/stopしない。
- nginxは実認証境界だが、Everything単体の組込み認証を検証したものではない。
- HTTPS/TLS、OAuth、インターネット上のSaaS MCP、本番credentialは検証していない。
- 新protocolのMRTR等は既存conformanceの証跡であり、この旧protocol実接続の結果に含めない。
- 会話へのLLM routingは#182、自作Addon固有受入は#221の範囲。
