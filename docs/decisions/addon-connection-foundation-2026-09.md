# 外部能力としてのAddonとMCP-first接続基盤（ドラフトADR）

- 作成日: 2026-09-05
- 最終整理日: 2026-09-06
- 文書段階: 作業ブランチ上の未受入ドラフト
- 状態タグ: 未付与。#152のcontract/fixtureレビュー後に`ACTIVE`へ移行する。
- 対象: #104 Addon接続基盤、#152 設計確定

> 本ADRは2026-09-05〜06のgrillMeと、その後のレビューで確定した設計判断を記録する。
> MCPを第一の公開・接続方式とし、HTTP/WS等の独自経路はMCPで満たせない具体的要件が確認された場合だけ追加する。
> Task/Event/副作用回復/長時間自律行動の詳細実装は後続Issueへ残し、#104では抽象境界までに留める。

## 1. 決定概要

### 1.1 MCP-first

自作Addonは独立したdomain serviceとして実装し、AI向け公開口はMCPを第一候補とする。Core側の初期ConnectorはMCPだけ実装する。

- 自作Addon: MCP `2026-07-28`基準、Streamable HTTP。
- 外部MCP: Streamable HTTP + stdio。採用SDKが対応する旧revisionも利用可能。
- 独自REST/WS Connector: 初期非対応。MCPで不足する実測要件が出た場合だけ追加する。
- MCPは公開口であり、ゲーム実行管理、domain transaction、競合制御、停止、履歴正本等のAddon内部責務を置換しない。

### 1.2 外部MCPを独自能力モデルへ完全変換しない

外部MCP Tool/Resourceはdigital-souls固有のQuery/Command/Task/Eventへmappingできなくても利用可能とする。

- mapping不能は利用不可理由にしない。
- native MCP schema/resultを保持する。
- 共通化するのは接続、実行metadata、policy適用に必要な最小情報だけ。
- Task/Event等へ安全に統合できる能力だけ追加統合する。

### 1.3 self-owned AddonはMCP標準 + namespaced metadata

- MCP標準で表現できる`annotations`、`execution`等は標準項目を優先する。
- digital-souls固有情報は`_meta["io.digital-souls/operation"]`に置く。
- metadata schemaの型/意味は共通基盤が定義する。
- operation固有のeffect/conflict/idempotency等の値はAddonが宣言する。
- Core overrideは安全側へ制限するものだけ許可する。

## 2. Trustとpermissionを分離する

### 2.1 connection grant

MCP連携は、登録済み`connection_instance`が提供する**検証済みactive Capability Snapshot内のTool利用許可**とする。

```text
user links connection
  ↓
discovery / static capability load
  ↓
schema + compatibility + identity validation
  ↓
staged snapshot
  ↓ execution loop boundary
active snapshot
  ↓
Tool/Resource利用可能
```

- connection grantは`validated_snapshot`単位で評価する。
- refreshで追加/変更されたToolは検証後にstaged snapshotへ入り、**次のexecution loop**からactiveになる。
- 現在のloop途中へ新Toolを挿入しない。
- 同一connection identity内の新Toolに、既定ではToolごとの再承認を要求しない。
- 接続先identityが変わった場合はrefreshではなく再連携とする。
- 将来Tool allowlist/snapshot固定grantが必要になれば拡張できるが、#104 MVPには入れない。

### 2.2 annotation trust

connectionの利用許可と、serverが自己申告するTool annotations/metadataを信頼することは別概念とする。

MCP Tool annotationsはhintであり、trusted server由来でない限り実行判断の根拠にしない。

- 外部MCPの`annotation_trust`既定はfalse。
- 登録時に明示されたconnectionだけannotationsをtrustedとして扱える。
- `readOnlyHint=true`でもannotation未信頼ならeffective effect=`unknown`。
- 並列化/Connector-level 1 retryは**trust評価後のeffective read-only**に限定する。
- `idempotentHint`等も未信頼serverからの申告だけで再送根拠にしない。

### 2.3 self-owned metadata trust

self-owned Addonのnamespaced metadataも無条件には信頼しない。

trustedになる条件:

1. Coreに登録済みconnection identityと実接続先が一致する。
2. service authenticationに成功する。
3. `contracts/addon/addon-meta.schema.json`検証に成功する。
4. `connection.trust.addon_metadata=true`である。

MCP `serverInfo`等の自己申告identityは表示/診断には使えるが、security identityの正本にしない。

## 3. CoreとAddonの責務

| 領域 | Core | Addon / 外部system |
|---|---|---|
| 状況理解 | 会話・記憶・知覚・外部情報を統合 | 構造化state/history/root evidenceを提供 |
| 行動判断 | 目標、優先順位、Tool選択、停止方針 | 独断で別の戦略目標を選ばない |
| 実行 | grant/binding/budget/policy確認後に依頼 | domain条件再検証、具体操作、pathfinding等 |
| 正本 | action監査、会話、人格の経験 | domain事実、Task/Event実状態 |

外部MCPはCoreのコード、設定、Character Card、credential、会話DB、記憶DB等を直接変更しない。
MCP本文を命令として扱い、Coreに同じ変更を代行させる迂回も許可しない。

一方、MCP結果をCoreが通常の会話に利用し、既存memory policyによって経験/記憶を形成することは許容する。

自作Addonは同一repoでも別process・別runtime設定・別domain storeとし、Core内部module/DBの直接importを連携手段にしない。
Addon process lifecycleはsystemd/Docker等の外部supervisorが所有し、Coreは通常Tool経路からspawn/kill/restartしない。

## 4. runtime構成

```text
Conversation / Character decision
        ↓
#182 Tool Catalog / tool-routing / binding resolution
        ↓
#104 source model + binding validation Port
        ↓
#104 Execution Gate
  - connection grant
  - effective effect / trust
  - concurrency / retry
  - budget / rate limit
  - stop / audit metadata
        ↓
MCP Client / Connector
  ├─ Streamable HTTP
  └─ stdio
        ↓
self-owned Addon MCP / external MCP
        ↓
domain service / external system
```

## 5. Static ManifestとCapabilitySource

初期Manifestは1ファイルでよいが、schema上は3領域を分離する。

```yaml
connection:
  # Core-owned: endpoint / transport / secret_ref / identity / trust / grant
capabilities:
  # 初期static。将来MCP/Addonから動的取得へ置換
core_policy:
  # Core-owned: enabled / sharing / binding requirement / budget / restrictions
```

取得元は`CapabilitySource`で抽象化する。

```text
StaticManifestSource ─┐
                      ├→ validated Capability Snapshot → Registry
McpCapabilitySource ──┘
```

#104ではStatic ManifestとMCP discoveryを扱い、#186でremote Manifest/release配布へ拡張する。

Manifestへraw secretを保存せず`secret_ref`だけ保持する。

## 6. Capability Snapshot / refresh

connection instanceごとに検証済みCapability Snapshotを持つ。

- 接続開始/再接続時にrefresh。
- ユーザー明示またはLLMの通常判断から明示refresh可能。
- MCP本文の指示だけではrefreshしない。
- `ttlMs`等はcache hint。TTL切れだけでactive snapshotを自動変更しない。
- refresh結果はstaged snapshotとして検証し、次execution loopからactivateする。
- Tool追加/削除/schema変更をdigestで検知する。
- cached snapshotは差分/診断用。接続不能時の実行根拠にしない。

### operation change

- 新Toolは基本MCP contractを満たせばCoreがdomain意味を知らなくても利用可能。
- unknown optional metadataは無視できる。
- `requires_core_features`等で必須機能を宣言しCore未対応なら、そのoperationだけ`unsupported`。
- removed Toolはrefresh activation後から新規実行不可。別Toolへ自動置換しない。
- 外部MCPは`connection_instance + tool_name + schema_digest`でdefinition revisionを追跡する。
- self-owned AddonはMCP Tool名とは別にstable operation IDを持つ。

## 7. MCP対応範囲とSDK

### 7.1 SDK pin

#152時点の基準:

```text
Python SDK: mcp==2.0.0
MCP protocol baseline: 2026-07-28
Python: 3.10+
```

適合記録は`docs/addon-mcp-sdk-compatibility-2026-09.md`を正本とする。
SDK package versionとMCP protocol versionは別々に記録する。

### 7.2 初期MCP能力

- Tools: discovery + call。
- Resources: discovery + read。
- Prompts: discovery可能。明示選択したテンプレートとしてのみ利用し、system/developer instructionへ自動昇格させない。
- Sampling: 初期非対応。2026-07-28ではdeprecatedであり、新規採用しない。
- Tasks: #104ではunsupported Port。Python SDK v2でTasks extensionが未実装のため、Task必須operationだけ`unsupported`へ落とす。同serverの通常Tool/Resourceは継続利用可能。

### 7.3 MRTR

2026-07-28ではserver→client callbackを前提にしない。

```text
original request
  ↓
InputRequiredResult
  - resultType=input_required
  - inputRequests
  - opaque requestState
  ↓
Core/userから回答収集
  ↓
original requestをinputResponses + requestState付きで再実行
```

- `requestState`はopaque/untrustedとして扱う。
- Coreが既存許可内で回答可能なものはCore/LLMが回答可能。
- user input/new permission/secret/Core高影響変更が必要ならuser interactionへ委譲する。
- MRTRの無限round-tripを許可しない。具体的round上限は#154/#156でSDK behaviorとExecution budgetを合わせて設定する。

## 8. HTTP auth / transport security

### 8.1 MVP auth

HTTP MCPの初期対応:

```text
none
preconfigured Bearer via secret_ref
```

- self-owned Streamable HTTP AddonはBearer service token必須。
- external HTTP MCPは`none`または事前設定Bearer。
- OAuth/OIDC/CIMD/DCR等interactive authorizationは#104 MVP非対応。
- OAuth必須serverは`unsupported_auth`。
- OAuth需要が具体化した時点でtoken persistence/issuer/scope/refresh/redirect UXを別Issue化する。

### 8.2 self-owned localhost server

Streamable HTTPのDNS rebinding対策を受入条件にする。

- 既定bindは`127.0.0.1`。`0.0.0.0`を既定にしない。
- incoming `Origin`を検証し、不正Originを403で拒否する。
- localhostでもservice authを有効にする。
- secret/endpoint/raw auth errorをLLM/Frontend/通常logへ露出しない。

## 9. connection_instance / sharing / binding

同じendpointでもcredential/auth主体が違えば別`connection_instance`とする。secret値そのものをIDには使わない。

security identityは登録済みendpoint、auth主体、TLS issuer等を基準とする。

sharing scope:

```text
shared
character-bound
user-bound（将来）
```

connection grantとresource bindingは分離する。

- grant: このconnectionのvalidated active snapshotを利用してよいか。
- binding: conversation/characterがどのrepository/player/match等を操作対象にするか。

新規endpoint/credential登録/再連携はユーザー操作。LLMは接続提案まで可能。

## 10. Execution Gate

### 10.1 effective effect

実行制御はnative annotationそのものではなく、trust評価後の`effective_policy`で行う。

```text
trusted read-only
  → parallel可
  → Connector-level read retry 1回可

write / destructive / external_send / unknown
  → same connection_instance内serial
  → automatic retryなし
```

未信頼`readOnlyHint=true`はunknownとして扱う。

### 10.2 budget

初期既定:

```text
max_calls_per_loop = 6
max_consecutive_same_tool = 3
max_identical_call = 2
normal_max_auto_cycles = 3
```

- budgetはconnectionごとにoverride可能。
- global / connection_instance / character・execution sessionの3層rate limitを設計可能にする。
- budget到達はTool failureと区別し、一度loopを終了して次のLLM判断へ制御を戻す。
- 明示的長時間自律sessionは別budgetとし、#104では実装しない。

### 10.3 retry

- effective read-only Tool/Resource/capability refresh: timeout範囲内で1 retryまで。
- state-changing/unknown: automatic retryなし。
- replay/idempotency固有契約は#185 `ActionRecoveryPort`で扱う。

### 10.4 user stop

ユーザー停止を最優先する。

1. 対象connection/resourceへの新規state-changing callを止める。
2. cancel対応Taskならcancel request。
3. cancel非対応なら新規操作を止め、追跡だけ継続。
4. 外部処理停止済みと推測せず状態を再照会。
5. 停止解除は新しいユーザー指示または明示Core policy。

## 11. Result境界

共通化するのはprotocol非依存のExecution envelopeまでとし、MCP native payloadを保持する。

```text
execution_id
connection_instance_id
definition revision
operation ref
started_at / finished_at
outcome / common error category
retry_count
native MCP payload
```

MCP text/structuredContent/image/resource link等を単一domain JSONへ無理に変換しない。
Tool実行成功後に追加metadata統合だけ失敗してもToolを再実行しない。

## 12. Task / Event / ActionRecoveryはPortまで

#104では次の抽象境界だけ用意する。

```text
TaskTrackerPort
EventSourcePort
ActionRecoveryPort
ConfirmationPolicyPort
```

### Task

- 将来はMCP Tasksを第一選択候補とする。
- #104時点ではPython SDKがTasks extension未実装なのでunsupported。
- CoreはTaskのdomain正本にならない。将来はtask ref + minimum metadataだけ保持する。

### Event

- Event本文のdomain正本はAddon側。
- cursor付きhistory Query/Resourceを復旧baselineとする。
- notificationはwake-upに使えても唯一の正本配送路にしない。
- #187でingestion cursor / consumer cursor / reconnect / backpressureを実装する。

### ActionRecovery

- stale/競合と`result_unknown`を分ける。
- 安全なreplay/status query契約がないstate-changing requestを自動再送しない。
- 詳細は#185。

## 13. #104と#182の責務分離

### #104が所有

- CapabilitySource / Static Manifest。
- MCP discoveryから得たnative source model。
- validated Capability Snapshot / revision / digest。
- `connection_instance`。
- binding**検証Port**。
- Execution Gate。
- MCP Client / transport。
- common Execution envelope / native result保持。

### #182が所有

- Tool Catalog生成。
- candidate絞り込み。
- LLM向けschema projection/token budget。
- binding**解決**。
- `tool-routing` / ToolDecision。
- sanitized current-turn result統合。

#104は「Catalogへ渡せるsource model」を提供するが、common Tool Catalog projection自体は生成しない。

## 14. Contract

正本:

```text
contracts/addon/manifest.schema.json
contracts/addon/addon-meta.schema.json
contracts/addon/capability-snapshot.schema.json
contracts/addon/execution-envelope.schema.json
contracts/addon/fixtures/
```

主要invariant:

- raw secret拒否。
- Core overrideは安全側のrestrictionだけ。
- 未信頼annotationからtrusted read-onlyへ昇格不可。
- `effect_source=unknown`ならeffect=unknown / serial / retry=none。
- stable operation IDとMCP Tool名を分離可能。
- native payloadをExecution envelope内で保持可能。

JSON Schemaだけで表現できないidentity照合、Core restrictionの単調性、logging非漏えい等はsemantic validation + #159 conformanceで検証する。

## 15. conformance

#159ではtest-owned別process MCPを使う。

- self-owned Streamable HTTP + Bearer auth。
- Origin拒否 / localhost bind設定。
- external stdio fixture。
- Tools / Resources / Prompts discovery。
- mapping不能Tool。
- annotation trusted/untrusted。
- Tool追加/削除/schema変更、snapshot activation境界。
- effective read-only並列/1 retry。
- state-changing/unknown serial/no retry。
- budget 6 / 3 / 2、normal 3 cycles。
- native result保持。
- Tasks必須operationをunsupportedへ落とし通常Toolを壊さない。
- MRTR `input_required` round-trip。
- secret/endpoint/raw payload非露出。

## 16. Issue境界

| Issue | 責務 |
|---|---|
| #104 / #152 | MCP-first共通基盤とcontract |
| #153 | Static Manifest、CapabilitySource、Registry、snapshot、trust model |
| #154 | MCP Client/Connector、Streamable HTTP/stdio、MRTR、Bearer auth |
| #156 | Manager / Execution Gate、effective policy、budget/concurrency |
| #159 | test-owned MCP conformance |
| #182 | Tool Catalog projection、routing、binding解決、LLM result統合 |
| #185 | confirmation、ActionRecovery、result_unknown |
| #187 | EventSource、cursor、reconnect/backpressure |
| #183 | Event会話通知・自発発話 |
| #184 | runtime管理UI / availability表示 |
| #186 | 動的Manifest、Addon配布/更新、別repo SDK |
| #58 | Development Observer MCP Addon |
| 後続 | 長時間自律行動session、Task高度化 |

## 17. 参考

- MCP 2026-07-28: https://blog.modelcontextprotocol.io/posts/2026-07-28/
- MCP Tools trust guidance: https://modelcontextprotocol.io/specification/draft/server/tools
- MCP Streamable HTTP security: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- MCP Python SDK: https://pypi.org/project/mcp/
- Python SDK roadmap: https://github.com/modelcontextprotocol/python-sdk/blob/main/ROADMAP.md
- Python SDK protocol versions: https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/protocol-versions.md
- SDK適合記録: `docs/addon-mcp-sdk-compatibility-2026-09.md`
- Testing policy: `docs/testing-policy.md`
- NIKE ROYALE AI参加ガイド: 2026-09-05にユーザーが共有した本文を設計事例として参照。実サービス接続は未検証。

## 18. 状態

設計判断、JSON Schema、fixture、SDK/versionの文書適合確認は完了した。

ADRを`ACTIVE`へ移行し#152をcloseする前に、`docs/testing-policy.md`へAddon conformance分類を反映し、レビューでcontract/fixtureの整合を確認する。
実MCPとのruntime conformanceは#154/#159で行い、#152の設計文書だけを実接続成功の証跡とは扱わない。
