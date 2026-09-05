# 外部能力としてのAddonとMCP-first接続基盤（ドラフトADR）

- 作成日: 2026-09-05
- 最終整理日: 2026-09-06
- 文書段階: 作業ブランチ上の未受入ドラフト
- 状態タグ: 未付与。#152のcontract・fixture・受入レビュー後に`ACTIVE`へ移行する。
- 対象: #104 Addon接続基盤、#152 設計確定

> 本ADRは2026-09-05〜06のgrillMeで確定した設計判断を記録する。
> 今回はMCPを第一の公開・接続方式として実装し、HTTP/WS等の独自経路はMCPで満たせない具体的要件が確認された場合だけ追加する。
> Task/Event/副作用回復/自律行動の詳細実装は後続Issueへ残し、今回は抽象境界までに留める。

## 1. 決定概要

### 1.1 MCP-first

自作Addonは独立したdomain serviceとして実装し、AI向けの公開口はMCPを第一候補とする。
Core側の初期ConnectorはMCPのみ実装する。

- 自作Addon: MCP 2026-07-28を基準にStreamable HTTPで公開する。
- 外部MCP: Streamable HTTPとstdioを初期対応する。採用SDKが正式対応する旧protocol versionも可能な範囲で接続する。
- HTTP API/独自WebSocket: 初期実装しない。必要性が実測・要件で確定した場合にConnectorを追加する。
- MCPを使ってもゲーム実行管理、domain transaction、競合制御、停止、記録等はAddon内部の責務として残る。MCPは公開口でありdomain実装を置換しない。

### 1.2 外部MCPは独自能力モデルへ完全変換しない

外部MCPのTool/Resourceは、digital-souls固有のQuery/Command/Task/Event分類へmappingできなくてもMCPとして利用可能にする。

- MCP連携の成立を、そのconnection instanceのMCP能力を利用する許可として扱う。
- mapping不能を理由にToolを利用不可にしない。
- 未分類Toolは副作用不明として保守的な並列制御・retry方針を適用する。
- MCP結果本体を単一`AddonResult.data`へ潰さず、native MCP payloadを保持する。
- 共通Task/Event等へ統合できる能力だけ追加統合する。

### 1.3 自作Addonは追加metadataで深く統合する

自作Addonは通常のMCPクライアントからも利用できるMCP互換性を維持しつつ、digital-souls固有metadataで実行管理を強化する。

- MCP標準で表現できる`annotations`、`execution`等は標準項目を使う。
- digital-souls固有情報はnamespaced `_meta`へ置く。
- metadata schemaの型・意味はCore/Add-on基盤が定義する。
- 各operationのdomain上の分類値はAddonが宣言する。
- Coreは宣言を検証し、安全側へ制限するoverrideだけを許可する。Addonの安全制約を緩和するoverrideは行わない。

## 2. 責務境界

| 領域 | Core | Addon / 外部システム |
|---|---|---|
| 共通知覚 | PC画面認識、音声等 | 外部固有観測を提供 |
| 状況理解 | 会話・記憶・知覚・外部情報を統合 | 構造化状態・履歴・根拠を提供 |
| 行動判断 | 目標、優先順位、行動選択、停止方針 | 独断で別の戦略目標を選ばない |
| 実行 | 利用許可・binding・budgetを確認して依頼 | domain条件再検証、具体操作、pathfinding等 |
| 即時反応 | 許可範囲・方針を設定 | 許可された回避・停止・応戦等を実行 |
| 正本 | action監査、会話、人格の経験 | 外部domain事実・実行状態 |

外部MCPはCoreのコード、設定、Character Card、credential、会話DB、記憶DB等を直接書き換えない。
MCP結果を命令として扱い、Coreに同じ変更を代行させる迂回も許可しない。
一方、MCPで得た経験をCore自身が既存の記憶ポリシーに従って会話・人格記憶へ反映することは許容する。

自作Addonは同一repo内でも別process・別DB/正本・別runtime設定とし、`backend/app`内部moduleやDBを連携手段として直接importしない。
将来別repositoryへ切り出せる公開境界を維持する。

## 3. runtime構成

```text
Coreの状況理解・行動判断
        ↓
Tool Catalog / tool-routing (#182)
        ↓
共通Execution Gate
  - connection/resource binding
  - permission / concurrency
  - budget / rate limit
  - audit metadata
        ↓
MCP Client / Connector
  ├─ Streamable HTTP
  └─ stdio（外部MCP）
        ↓
MCP Server
  ├─ 自作Addon
  └─ 外部/コラボMCP
        ↓
外部system / domain service
```

自作Addon processのownerはCoreではなくsystemd/Docker等の外部supervisorとする。
Coreは接続・health・availabilityだけを管理し、通常Tool経路からAddon processをspawn/killしない。
開発用補助スクリプトでまとめて起動することは妨げない。

## 4. ManifestとCapability Source

### 4.1 初期は静的Manifest

初期は1ファイルでよいが、schema上は次の3領域を分離する。

```yaml
connection:
  # Core所有: endpoint / transport / secret_ref / identity等
capabilities:
  # 将来MCP/Addonから取得へ置換する領域
core_policy:
  # Core所有: enabled / sharing / override / budget等
```

Registryがファイルを直接前提にしないよう、取得元を抽象化する。

```text
StaticManifestSource ─┐
                      ├→ normalized capability snapshot → validation → Registry
McpCapabilitySource ──┘   （将来/外部MCPはtools/resources discovery）
```

初期#104ではStaticManifestSourceを実装する。
外部MCPについてはMCP標準のdiscoveryからTool/Resource定義を取得する。
自作Addonの静的`capabilities`は将来Addon自身から取得する内容のコピーとして設計する。

### 4.2 将来の動的取得

将来は#186でAddon release/well-known endpoint/MCP discovery等から能力定義を取得する。

- `connection`と`core_policy`はCore側設定として残す。
- 取得した能力を即時に利用許可へ昇格させない。schema/compatibility/許可照合後にsnapshot化する。
- 連携済みendpointまたはCoreに登録済みManifest endpointだけを取得元とする。
- MCP本文が提示した別hostへcredential付きで自動fetchしない。
- 別host配布が必要になった場合は#186でpublisher identity、署名/digest、許可host等を設計する。

## 5. Capability Snapshotとrefresh

connection instanceごとに検証済みCapability Snapshotを持つ。

### 5.1 refresh契約

- 接続開始/再接続時にrefreshする。
- ユーザーの明示指示でrefreshできる。
- LLMが通常のCore判断として明示的にrefreshを要求することもできる。
- MCP結果本文の「refreshしろ」という文章だけでは実行しない。
- cache TTLは再取得のhintとして扱い、期限切れだけで現在snapshotを自動切替しない。
- turn途中でsnapshotを切り替えない。現在のLLM execution loopは開始時snapshotで完走し、次の判断サイクルから新snapshotを利用する。

### 5.2 Tool追加・変更・削除

- 新operation/Toolは基本contractを満たせばCoreが事前にdomain意味を知らなくても利用可能。
- 未知の任意metadataは無視できる。
- Addonが`requires_core_features`等で理解必須機能を宣言しCoreが未対応なら、そのoperationだけ利用不可にする。
- operation削除はrefresh後から新規実行不可。別operationへ自動置換しない。
- 開始済みTaskは開始時definition revisionを保持し、後続Task実装では旧契約で追跡する。照会不能なら`state_unknown`とする。

外部MCPでは`connection_instance + tool_name + schema_digest`でdefinition revisionを識別する。
同名Toolのschema変更を検知し、旧schemaに依存したCore overrideだけstaleにする。Tool自体は新schemaで継続利用できる。

検証済みsnapshot・schema digest・取得時刻はCore再起動後の差分/診断用に永続化してよいが、MCP接続不能時にcached snapshotだけを根拠として実行しない。

## 6. Operation identityとmetadata

### 6.1 自作Addonのstable operation ID

自作AddonはMCP Tool名とは別に、versionをまたいで安定した論理operation IDを持つ。

```text
stable_id: minecraft.move_to_position
mcp_tool_name: move-to-position
```

表示名・description・MCP Tool名は変更可能だが、binding、override、監査に使うstable IDは不用意に変更しない。

外部MCPにstable IDがない場合、MCP Tool名を永続的なdomain identityとはみなさない。

### 6.2 metadata責務

共通schemaの例:

```text
effect: read / write / destructive / external-send / unknown
execution: immediate / task
conflict_scope
concurrency
idempotency / replay semantics
source / freshness metadata
requires_core_features
```

これらを排他的なQuery/Command/Task/Event enumへ強制しない。
Task/Eventは直交する実行特性として扱う。

Addonがoperation固有の意味を宣言し、Coreが「その意味をどう扱うか」を決める。
CoreがTool名/descriptionをLLMで推測して正規分類を作ることを必須にしない。

## 7. MCP対応範囲

### 7.1 初期対応

- Tools: 正式対応。Tool discoveryとcallを実装する。
- Resources: 正式対応。外部データとして参照する。
- Prompts: 取得可能にしても明示選択時だけ利用し、system/developer instructionへ自動昇格させない。
- Sampling: 初期非対応。外部MCPにLLM/Provider利用判断を委ねない。
- MRTR/Elicitation等: MCP Client側で受けられる境界を用意する。Coreが既存許可範囲内で回答可能ならCore/LLMが回答し、ユーザー入力・新権限・secret・高影響Core変更が必要ならユーザーinteractionへ委譲する。

### 7.2 Tool選択

Addon operationとMCP Toolは、選択用の最小projectionだけ共通Tool Catalogへ載せ、#182の`tool-routing`で選択する。

共通projection例:

```text
source_id
connection_instance_id
tool/operation id
name / description
input schema projection
binding / availability
```

元のMCP schemaをRegistryに保持し、実行時は元schemaで再検証する。
Provider native tool callingへMCPだけ直結する別ループは初期採用しない。

## 8. Connection identity・sharing・binding

### 8.1 connection instance

同じendpointでもcredential/auth主体が違えば別`connection_instance`とする。
secret値そのものをIDには使わずCore内部の安定登録IDを割り当てる。

Security identityは登録済みendpoint、認証主体、TLS/issuer等を基準にし、MCP `serverInfo`の自己申告だけでsecurity identityを判定しない。

接続先identityが大幅に変わった場合はrefreshではなく再連携とする。

### 8.2 sharing

connectionごとにsharing scopeを設定する。

```text
shared
character-bound
user-bound（将来）
```

Development Observerはsharedを基本とし、個人ゲームplayer token等はcharacter-boundを基本とする。

### 8.3 resource binding

MCP接続許可とresource bindingを分ける。
MCP連携はそのconnectionのTool利用許可、bindingはconversation/characterがどのrepository/player/match等を扱うかを示す。

## 9. 認証・secret

自作AddonのStreamable HTTPもlocalhostだから無認証とはせず、サービス間認証を必須にする。
初期はBearer等のサービスtokenでよく、mTLSは別host/運用要件が出た段階で追加検討する。

Manifestには生secretを置かず`secret_ref`だけ保持する。
Connectorが送信直前に環境変数/credential store等から解決する。
secret、endpoint、raw auth errorをLLM、Frontend、通常logへ露出しない。

新しいMCP connectionのendpoint登録・credential設定・再連携はユーザー操作とする。
LLMは接続提案まで可能。一度連携済みのMCPについては許可範囲内でrefresh・Tool利用できる。

## 10. 共通Execution Gate

### 10.1 permission

外部MCPは「連携＝MCP Tool利用許可」とするため、domain副作用ごとの都度確認をMCP初期経路の必須条件にしない。
ただしCore自身の保護境界は維持する。

自作AddonのmetadataやCore overrideは、並列性・競合・将来の高影響policyを強化する補助情報として使う。
#185のdestructive/external-send等の詳細confirmation policyは後続実装に残す。

### 10.2 concurrency

初期既定:

- read-onlyと明示されたTool/Resource: 並列可。
- state-changingまたは分類不能Tool: 同一connection instance内で直列化。
- 将来、resource/conflict scopeが安全に取得できる自作Addonはscope単位へ細分化可能。

同じ対象への新Commandが既存Taskを暗黙cancel/置換しない。
Addon側も実行直前のdomain条件を再検証する。

### 10.3 execution budget

1回のLLM Tool execution loopの初期既定値:

```text
max_calls_per_loop = 6
max_consecutive_same_tool = 3
max_identical_call = 2
```

通常会話の自動判断サイクルは最大3サイクルを既定とする。
明示的な自律行動sessionは別のsession budgetを使い、通常会話の無制限延長にはしない。

budgetはconnectionごとにoverride可能。global / connection_instance / character・execution sessionの3層rate limitを持てる設計にする。

budget到達時はTool loopを一度終了し、結果をまとめて次のLLM判断サイクルへ進める。自動でbudget自体を増やさない。

### 10.4 retry

- read-only Query/Resource/capability refresh: Connectorレベルで1回まで自動retry可能。
- state-changing/分類不能Tool: 原則自動retryしない。
- 接続先が同一依頼の安全なreplay/idempotencyを明示的に保証する場合のみ、その契約を後続ActionRecoveryで利用する。
- Task status等のread-only照会: 1回retry可能。

同一Tool＋同一引数のretryが安全回復契約によるものか、LLMの反復かを区別する。

### 10.5 user stop

ユーザー停止を最優先する。

1. 対象connection/resourceへの新規Commandを停止する。
2. 実行中Taskがcancel対応なら取消要求する。
3. cancel非対応なら新規操作を停止したまま追跡だけ続ける。
4. 外部処理が停止済みと推測せず最終状態を再照会する。
5. 停止解除は新しいユーザー指示または明示Core policyで行う。

## 11. Result境界

共通化するのは実行metadataまでとし、payload本体はnative形式を保持する。

共通Execution envelope例:

```text
execution_id
connection_instance_id
source/tool/operation id
definition revision
started_at / finished_at
outcome
transport/protocol-independent error category
```

MCPのtext、structuredContent、image、resource link等を無理に単一JSONへ変換しない。
LLM用projection、UI表示、Task/Event統合が必要な場所だけ変換する。

MCP Toolの実行自体が成功した後にdigital-soulsの追加metadata統合が失敗しても、Tool失敗として再実行しない。

## 12. Task / Event / Action Recoveryの境界

今回#104で詳細実装はしない。将来実装を差し替えられる抽象Portだけ用意する。

```text
TaskTrackerPort
EventSourcePort
ActionRecoveryPort
ConfirmationPolicyPort
```

### 12.1 Task方針

自作Addonの長時間処理はMCP Tasksを第一選択とし、Core TaskManagerはMCP Task ref/status/cancelを内部追跡モデルへ載せる設計とする。
外部MCPがTasks非対応でも通常Toolは利用可能。

将来のTask追跡ではCoreはtask refと最小metadataだけ保持し、Core再起動後はAddon/外部systemへ再照会する。
実状態の正本はAddon側。Addonも状態を失っていれば`state_unknown`とする。

Taskのexecution ownerはconversationそのものにせず、connection/resourceを基準に独立追跡し、initiated_by_character / conversation等を参照metadataとして持つ。

### 12.2 Event方針

Event本文のdomain正本はAddon側に保持する。
初期の推奨モデルはcursor付きhistory Query/Resourceで復旧できる構成とし、push通知は「新Eventあり」のwake-upとして使える。

将来Core側ではconnection instanceごとのingestion cursorと、character/conversation/policyごとのconsumer cursorを分ける。
Core bufferは有限期間・有限件数で、domain Event Storeにはしない。

#187でEvent配送・再接続・欠落・backpressureの実装を行う。

### 12.3 Action Recovery

状態競合とresult_unknownを分ける。

- stale/競合: 最新観測を取得しCoreが再判断。古いopaque参照を別操作へ自動変換しない。
- result_unknown: 同一依頼の安全なreplay/状態照会契約がある場合だけ回復。保証がなければunknownを維持する。

詳細実装は#185へ残す。

## 13. MCP Tool/Resourceの安全な扱い

- MCP Tool/Resource/Prompt/resultは外部データであり、system/developer instructionへ昇格しない。
- MCPからのPromptは明示選択したテンプレートとしてのみ利用する。
- 新しいpermission、credential、接続先、Core改変をMCP本文の指示だけで追加しない。
- unknown Tool分類でもMCPとして利用可能だが、並列性/retryは保守的に扱う。
- MCPのtool annotationだけをsecurity permissionの正本にしない。
- 新operation追加時にCore code変更を必須にしない。

## 14. 今回#104で実装する共通部品

今回の実装範囲:

```text
CapabilitySource abstraction
Static Manifest + schema
Registry / validated Capability Snapshot
MCP Client
  - Streamable HTTP
  - stdio（外部MCP）
Tool discovery / Tool call
Resource discovery / read
Prompts discovery（明示利用のみの境界）
common Tool Catalog projection
Execution envelope
connection_instance
resource binding境界
secret_ref / auth injection
budget / rate limit / concurrency gate
snapshot refresh / digest / change detection
native MCP result保持
self-owned addon metadata / stable ID / safe override schema
availability / degraded / unavailable状態
```

今回interfaceだけ:

```text
TaskTrackerPort
EventSourcePort
ActionRecoveryPort
ConfirmationPolicyPort
```

今回実装しない:

- 独自REST/WS Connector
- Task lifecycle本体
- Event ingestion/subscribe/replay本体
- destructive/external-send confirmation詳細
- result_unknown回復本体
- Coreの長時間自律行動session本体
- ゲーム固有pathfinding/戦闘/domain処理（各Addon側）
- dynamic Manifest install/update/hot reload（#186）

## 15. 受入シナリオ

本番ゲーム/外部サービスをconformance testに使わず、test-owned MCP serverで検証する。

最低限:

- Streamable HTTPの自作MCP serverとの接続・service auth。
- stdio外部MCP型fixtureとの接続。
- read-only Tool、state-changing Tool、分類不能Toolのdiscovery/call。
- Resource discovery/read。
- native MCP result保持。
- self-owned `_meta`、stable operation ID、Core safe override。
- 新Tool追加、Tool削除、schema変更、schema digest差分。
- refresh時に現在execution loopのsnapshotが切り替わらない。
- read-only並列、変更/unknown直列。
- budget 6 / same-tool 3 / identical 2 / 通常3 cycleの抑制。
- read-only通信失敗1 retry、変更Toolは自動retryなし。
- secret/endpoint/raw payloadを通常log/LLM/Frontendへ露出しない。
- 未対応Task/Event capabilityを認識し、通常Tool利用を壊さず明示的に未実装Portへ落とせる。
- MCP定義をdigital-souls独自Query/Commandへ完全mappingできないToolも利用可能。

## 16. Issue境界

| Issue | 責務 |
|---|---|
| #104 / #152 | 本ADRのMCP-first共通基盤とcontract |
| #153 | 3領域Static Manifest、CapabilitySource、Registry、snapshot |
| #154 | MCP Client/Connector（Streamable HTTP + stdio） |
| #156 | Manager/Execution Gate/connection instance共通境界 |
| #159 | test-owned MCP conformance |
| #182 | Tool Catalog projection、tool-routing、binding、LLMへの結果統合 |
| #185 | 副作用confirmation、ActionRecovery、result_unknown等の詳細実装 |
| #187 | EventSource、ingestion、cursor、reconnect/backpressure詳細実装 |
| #183 | Eventの会話通知・自発発話 |
| #184 | runtime管理UI / availability表示 |
| #186 | 動的Manifest取得、配布、更新、別repo SDK |
| #58 | Development Observer domain service / MCP公開実装 |
| 後続 | Coreの長時間自律行動session、Task高度化 |

## 17. 参考

- MCP Architecture: https://modelcontextprotocol.io/docs/learn/architecture
- MCP 2026-07-28 transport/specification: https://modelcontextprotocol.io/specification/2026-07-28/
- MCP Tasks: https://modelcontextprotocol.io/extensions/tasks/overview
- Repository policy: `docs/repository-policy.md`
- ADR運用: `docs/decisions/README.md`
- Testing policy: `docs/testing-policy.md`
- NIKE ROYALE AI参加ガイド: 2026-09-05にユーザーが会話で共有した本文。MCP/observe/act/history/action_ref/ACTION_STALE/replayed等を設計事例として参照。実サービス接続は未検証。

## 18. 状態

設計判断は共有理解に到達した。
#152をcloseする前に、上記contractのJSON Schema/fixture、MCP SDK/versionの実装適合、testing-policy反映を完了する。
ドラフト作成だけで#153以降を実装完了扱いにはしない。
