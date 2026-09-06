# 会話からの外部MCP利用

設計契約は[Tool利用ADR](decisions/tool-use-foundation-2026-09.md)、接続Manifestと
Execution Gateは[外部MCP利用基盤](external-mcp-foundation.md)を参照する。

## 設定

BackendにTool選択用のTargetと管理設定ファイルを指定する。既存のchat等の設定も必要。

```dotenv
INFERENCE_TARGET_TOOL_ROUTING=ollama/gemma4:e4b
INFERENCE_TARGET_TOOL_ROUTING_MAX_INPUT_TOKENS=12288
INFERENCE_TARGET_TOOL_ROUTING_MAX_OUTPUT_TOKENS=1024
INFERENCE_TARGET_TOOL_ROUTING_OPTIONS_JSON={"temperature":0}
INFERENCE_TARGET_TOOL_ROUTING_TIMEOUT_SECONDS=90
DS_MCP_CONFIG=/管理側で用意した絶対パス/mcp.json
```

Target未設定時はMCPへ自動接続せず、通常のテキスト・音声会話を維持する。
設定ファイルはBackendから読める場所へ置く。Composeでは既存のdata root等のmount内を使うか、
管理側の明示的なread-only mountを追加する。ホスト専用パスがコンテナ内でも読めると仮定しない。
stdioのcommandと依存packageもBackend実行環境で利用可能にする。

管理設定の外形:

```json
{
  "version": 1,
  "connections": [],
  "bindings": []
}
```

`connections`へ`contracts/addon/manifest.schema.json`形式のManifestを入れる。
Bearerは`secret_ref`でBackendの環境変数を参照し、ファイルへ生のsecretを書かない。
接続の所有者・trust・sharingは管理側が指定する。変更時はBackendを再起動し、identity変更は
#104の再連携契約に従って新しいconnection IDを割り当てる。

bindingが必要な接続だけ、次の形式で対象を指定する。

```json
{
  "id": "project-a",
  "connection_id": "registered-connection",
  "character_id": "miori",
  "label": "プロジェクトA",
  "operations": ["search_project"],
  "arguments": {"project": "project-a"}
}
```

`arguments`は対象を固定するための引数。LLMが異なる値を指定した場合は実行しない。
対象選択は会話内で保持し、会話終了・停止等で破棄する。

## 利用と状態

通常の会話から必要なTool／Resourceを選択する。追加情報が必要なら質問し、入力または音声で回答を受ける。
会話画面の「外部参照」で処理中・入力待ち・今回参照した情報を確認できる。
「外部操作を停止」は新規実行と入力待ちを停止する。実行開始済みの外部操作の取消しは保証しない。
テキストの処理中はHTTP切断を監視する。入力待ちの画面消失は終了通知または状態pollの45秒途絶で検出し、
音声の切断・明示停止と同様に待機を破棄する。入力待ち自体の上限10分はpollで延長しない。

内部APIは`GET /tool-use/status/{character}/{conversation_id}`と`POST /tool-use/stop`。
停止bodyは`character`と`conversation_id`。接続設定やnative payloadをAPIへ公開しない。

## 開発検証

```bash
backend/.venv/bin/python -m pytest backend/tests/unit/test_tool_use.py backend/tests/unit/test_external_mcp_gate.py
npm ci --prefix infra/testing/mcp-real-servers --ignore-scripts
RUN_TOOL_USE_REAL_SERVICE_TESTS=true \
MCP_REAL_SERVER_ROOT="$PWD/infra/testing/mcp-real-servers" \
backend/.venv/bin/python -m pytest -s backend/tests/integration/test_tool_use_real_service_integration.py
```

明示開始後の依存不足・接続失敗はfailとする。上記Backend実接続はLLMによる選択・実行の検証であり、
ブラウザ表示やLiveKit音声再生までの受入とは区別する。

ブラウザから実Backend・Ollama・Whisper・VOICEVOX・LiveKitを通す受入:

```bash
backend/.venv/bin/python scripts/acceptance_tool_use.py
backend/.venv/bin/python scripts/acceptance_tool_use.py --contract-mcp
```

最初のコマンドは公開Filesystem（stdio）とEverything（Streamable HTTP Resource）を使い、
テキスト回答と音声入力→外部取得→回答→実AudioTrack再生を検証する。
後者はCoreをimportしない制御用MCPを別processで起動し、MRTR質問・回答と遅延中の停止を検証する。
制御用MCPの成功を第三者MCPへの実接続証跡として扱わない。

どちらも既存のOllama・Whisper・VOICEVOXを共通推論サービスとして利用し、独立した一時data root、
Backend、Frontend、テスト所有LiveKit containerを起動する。Dockerと
`livekit/livekit-server:v1.9.7`、Playwright Chromiumが必要。既存processやdogfoodは停止しない。
マイク入力はVOICEVOXで作成した合成音声ファイルをChromiumへ渡す。実STTとWebRTC mediaは通るが、
物理マイク・人の発話品質を確認した証跡ではない。
制御MCPの音声試験ではWebAudioのMediaStreamへ合成発話を流し、追加質問の再生を観測して回答を投入する。
質問へのbarge-inから同じMCP操作の再開・最終回答再生までを検証し、STT/LLM/TTSや通信は置換しない。
結果は`frontend/test-results/tool-use-browser/`と`tool-use-contract/`、runtime logはそれぞれ
`tool-use-runtime/`と`tool-use-contract-runtime/`へ分けて出力する。

候補schema・結果は各4096 tokenを暫定上限とし、初期実装ではUTF-8 byte数で保守的に制限する。
推論全体の見積もりが上限を超えれば古いrouting履歴・下位候補を削り、最終回答の残量に合わせ結果を省略する。
最低限の省略通知も収まらない場合は、外部実行の前に入力上限エラーにする。
Tool処理は1回の入力につき最大120秒。長い処理を使うreverse proxyではこの期限と最終回答生成時間を許容する。
