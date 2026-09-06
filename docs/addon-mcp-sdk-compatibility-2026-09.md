# MCP Python SDK適合記録（#152）

- 確認日: 2026-09-06
- 対象: #104 / #152 MCP-first外部能力接続基盤
- 採用候補: `mcp==2.0.0`
- Protocol基準: MCP `2026-07-28`
- Backend要件: Python 3.10+

## 結論

#104の初期MCP Client/Connectorは公式Python SDK v2を利用し、実装時の依存を`mcp==2.0.0`へpinしてconformanceを行う。

PyPI上のstable lineはv2で、2026-07-28 specificationとそれ以前のrevisionへの接続をサポートすると公式説明されている。v2のClientは`mode="auto"`で2026-07-28の`server/discover`を試し、旧serverではinitialize-eraへfallbackする。

`mcp==2.0.0`は2026-07-28に公開されたstable releaseとして確認した。依存更新時は本記録のversionを書き換えるだけで済ませず、#159 conformanceを再実行する。

## 初期対応matrix

| 項目 | #104方針 | SDK/仕様確認 | 判定 |
|---|---|---|---|
| Streamable HTTP client | 必須 | Python SDK v2の標準target | 対応 |
| stdio client | 外部MCP向け必須 | Python SDK v2で対応 | 対応 |
| MCP 2026-07-28 | self-owned Addon基準 | v2のmodern protocol | 対応 |
| 旧MCP revision | 外部MCPで可能な範囲 | `Client(..., mode="auto")`がfallback | 対応 |
| Tools discovery/call | 必須 | SDKの基本機能 | 対応 |
| Resources discovery/read | 必須 | SDKの基本機能 | 対応 |
| Prompts | 明示利用境界のみ | SDK機能あり | 対応。ただし自動prompt injection禁止 |
| MRTR | 受理境界が必要 | 2026-07-28では`InputRequiredResult`→回答収集→元request再実行 | 対応設計 |
| Sampling | 初期非対応 | 2026-07-28でdeprecated | digital-soulsでは非対応 |
| Tasks extension | Portのみ | Python SDK v2では現時点未実装 | `TaskTrackerPort=unsupported` |
| OAuth | 初期非対応 | SDK機能の有無にかかわらずMVP範囲外 | `unsupported_auth` |
| preconfigured Bearer | self-owned/外部HTTPで利用 | HTTP header injectionとして実装 | #154で実装 |

## MRTRの扱い

2026-07-28ではserver→client JSON-RPC request channelを前提にしない。

```text
Core -> tools/call / resources/read / prompts/get
         ↓
server returns resultType=input_required
  + inputRequests
  + opaque requestState
         ↓
Coreが回答を収集
  - 既存許可内でCore/LLMが答えられるもの
  - user interactionが必要なもの
         ↓
元requestをinputResponses + byte-equivalent requestState付きで再実行
```

`requestState`はopaque/untrustedとして扱い、Core状態やsecurity identityへ展開・解釈しない。
MRTR自動round数には上限を設け、通常Execution budgetと別に無限往復を許可しない。具体値は#154/#156実装時にSDK既定とCore budgetを合わせて確定する。

## Tasks

MCP `2026-07-28`ではTasksは`io.modelcontextprotocol/tasks` extensionであり、core protocol必須機能ではない。
公式Python SDK v2 roadmapではTasks extensionは未実装とされている。

したがって#104では:

- Tool definition上でTask関連能力を認識してもよい。
- Taskが必須のoperationは`unsupported`へ落とす。
- `TaskTrackerPort`を用意するが実装しない。
- Task非対応を理由に同じserverの通常Tools/Resourcesまで利用不可にしない。
- SDKでTasksが実装された後に、後続IssueでMCP Tasksを第一選択として接続する。

## HTTP認証の初期範囲

#104 MVPでサポートするHTTP MCP認証は次だけとする。

```text
none
preconfigured bearer token via secret_ref
```

- self-owned Streamable HTTP AddonはBearer service token必須。
- external HTTP MCPは`none`または事前設定Bearer。
- OAuth/OIDC/CIMD/DCR等のinteractive authorization flowは初期非対応。
- OAuth必須serverは`unsupported_auth`として明示する。
- secret値をManifest、Capability Snapshot、通常log、LLM prompt、Frontendへ渡さない。

OAuth対応が具体的に必要になった場合は、token persistence、issuer/scope validation、redirect UX、refresh/rotationを別Issueで設計する。

## Streamable HTTP security

self-owned localhost serverの受入条件:

- 既定bindは`127.0.0.1`。`0.0.0.0`を既定にしない。
- incoming HTTPの`Origin`を検証し、不正Originは403で拒否する。
- localhostでもservice authenticationを有効にする。
- Host/Origin/auth failureを通常LLM contextへ入れない。
- remote deployment時はTLSと登録済みendpoint/issuer等のidentity照合を追加する。

これらはMCP Streamable HTTP仕様のDNS rebinding対策に合わせる。

## annotation trust

MCP Tool annotationsはhintであり、trusted server由来でない限り実行判断に使わない。

- 外部MCPのannotation trustは既定false。
- 明示設定されたconnectionだけannotationを信頼可能。
- self-owned Addonでも、登録済みidentity照合 + `addon-meta.schema.json`検証成功後だけmetadataを信頼する。
- 並列化/1 retryはtrust評価後のeffective read-onlyだけ。
- 未信頼`readOnlyHint=true`はeffective effect=`unknown`。

## version運用

- requirementsへ追加する際は`mcp==2.0.0`でpinする。
- version bumpはdependabot等による自動merge対象にせず、#159相当conformanceを要求する。
- protocol versionとSDK package versionを別の概念として記録する。
- `serverInfo`/discovery identityは表示・診断には使えるが、security identityの単独根拠にしない。

## 実装時の確認項目

#154/#159で実際に次を実行する。

1. `mcp==2.0.0`をinstallしたtest environmentを固定。
2. test-owned Streamable HTTP MCPへ`Client`で接続。
3. 2026-07-28 negotiation/discover、Tools、Resourcesを確認。
4. stdio test server接続を確認。
5. MRTR `input_required` fixtureを確認。
6. Tasks必須operationが`unsupported`となり通常Toolを壊さないことを確認。
7. 外部HTTPのnone/Bearer/auth failureを検証。self-owned Origin拒否・localhost bindは#221へ分離。
8. external legacy fixtureで`mode="auto"` fallbackを確認。

## 参照

- PyPI `mcp`: https://pypi.org/project/mcp/
- Python SDK v2 What's New: https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md
- Python SDK protocol versions: https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/protocol-versions.md
- Python SDK roadmap: https://github.com/modelcontextprotocol/python-sdk/blob/main/ROADMAP.md
- MCP 2026-07-28 release: https://blog.modelcontextprotocol.io/posts/2026-07-28/
- MCP Tools trust guidance: https://modelcontextprotocol.io/specification/draft/server/tools
- MCP Streamable HTTP security: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
