# Inference Provider基盤・Target設定契約 (2026-09)

## 状態

**ACTIVE**。Issue #105で採用したInference Provider基盤の設計上の正本であり、実装進捗は
親Issue #105と子Issue #188、#177、#178、#179、#180、#181で管理する。

本ADRの採用時点では旧Ollama用途別設定からの移行中である。実行環境が新契約へ切り替わるまでは
実装済みの運用手順を使用するが、設計または新規実装が本ADRと競合する場合は本ADRを優先する。
旧設定の暫定互換はrelease契約にせず、Issue #181で撤去する。

RAG privacyの絶対禁止、SQLiteをpersona memoryの正本、Chromaを派生Indexとする境界、character／
conversation／環境identityによるデータ分離、dogfoodのbackup／rollback、既存推論serviceのprocess
ownershipは置き換えない。

## 背景

Chatには共通LLM interfaceとRouterがあるが実運用はOllama固定であり、Semantic Privacy、Persona
Memory extraction／consolidation、EmbeddingにはOllama固有ClientまたはHTTP APIへの直接依存もある。
ProviderやModelを変更するとdomain logic、起動処理、readiness、テスト、環境設定を個別に変更する
必要がある。

Provider／ModelはGPU、endpoint、契約アカウント等のインフラ条件に強く依存する。具象Model一覧を
version管理された`inference.yaml`へ固定すると環境差分とsecret境界が混在する。独立Profile Registryは
用途Target、Profile、Providerの3段解決となり、単一用途の設定にも不要なID管理が生じる。

このためCoreが用途と安全要件をTargetとして所有し、環境側はTargetに対応する`provider/model`と
実行上限だけをenvで指定する。

## 用語

| 用語 | 意味 |
|---|---|
| Target | `chat`、`privacy`等のCoreが定義するInference用途 |
| Provider ID | Adapterとlocal／cloud区分を選ぶ固定キー |
| Model ID | Providerへ渡すModel識別子。Provider内部の`/`を含められる |
| Adapter | Provider固有transport、認証、Options、error変換を閉じ込める実装 |
| Capability | Targetが要求し、Adapterが実装する独立したInference操作 |
| Principal | Inferenceを要求したCore機能またはAddonの内部identity |
| Probe | 起動時に課金を伴わない範囲で行う到達性／認証／Model存在確認 |

環境オーケストレーターが使用するdev／dogfood等の実行ProfileとInference Targetを区別する。本ADRでは
Inference Profileという中間概念を導入しない。

## 決定事項

### 1. Targetから`provider/model`を直接解決する

```text
Core / Addon Context
        │ Target
        ▼
 Inference Router
        │ provider/model
        ▼
 Provider Registry
   ├─ ollama       -> Ollama Adapter
   ├─ openai-api   -> OpenAI API Adapter
   └─ openai-codex -> Codex runtime Adapter
```

- 上位層はProviderやModelではなくTargetを指定する。
- 独立したInference Profile Registryと具象的な`inference.yaml`は設けない。
- global default、暗黙のModel継承、自動Provider選択、自動fallbackを設けない。
- Provider ID一覧を別設定に重複させず、設定済みTargetからProviderを導出する。
- Adapterとlocal／cloud区分はコード内Provider Registryで解決し、envから自己申告させない。

Target参照は最初の`/`だけで分割する。左辺をProvider ID、残り全体をModel IDとし、Model ID内の追加
`/`は保持する。空のProvider／Model、前後の空白、未知Providerは起動時に拒否する。

MVPはProvider IDごとに接続設定と認証を1組だけ持つ。同一環境で複数Ollama endpointまたは複数OpenAI
accountを同時利用しない。将来用に`provider@instance/model`を予約し、MVPではProvider部に`@`があれば
未対応設定として拒否する。

### 2. Target名と安全要件をCoreに固定する

| Target | env token | 要求Capability | 起動時criticality |
|---|---|---|---|
| `chat` | `CHAT` | streaming text、token estimate | required |
| `privacy` | `PRIVACY` | structured generation、token estimate | degradable。ただしcloud設定はinvalid |
| `memory-extraction` | `MEMORY_EXTRACTION` | structured generation、token estimate | degradable |
| `memory-consolidation` | `MEMORY_CONSOLIDATION` | structured generation、token estimate | degradable |
| `embedding` | `EMBEDDING` | embedding、token estimate | degradable |
| `heavy-reasoning` | `HEAVY_REASONING` | text generation、token estimate | optional |

Target Registryが要求Capability、criticality、Callerの失敗時方針を所有する。envから追加、自己申告、
緩和できない。新TargetにはCore変更が必要だが、既存TargetのProvider／Model変更はenvだけで行える。

必須Targetの欠落は起動失敗、degradable Targetの欠落／接続不能は起動継続と`degraded`、optional Targetの
欠落は`unconfigured`とする。設定値が存在するが形式、Options、Capability、安全制約が不正な場合は
criticalityにかかわらず起動を失敗させる。

### 3. env schemaをTarget単位で固定する

```env
INFERENCE_TARGET_CHAT=ollama/gemma4:e4b
INFERENCE_TARGET_PRIVACY=ollama/gemma4:e4b
INFERENCE_TARGET_MEMORY_EXTRACTION=ollama/gemma4:e4b
INFERENCE_TARGET_MEMORY_CONSOLIDATION=ollama/gemma4:e4b
INFERENCE_TARGET_EMBEDDING=ollama/nomic-embed-text:latest
INFERENCE_TARGET_HEAVY_REASONING=openai-codex/<configured-model>

INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS=24576
INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS=8192
INFERENCE_TARGET_HEAVY_REASONING_OPTIONS_JSON='{"reasoning_effort":"high"}'
```

上記は既定／dogfoodで既存挙動を維持する例である。同じ環境でextractionを`gemma4:e4b`、
consolidationを環境側に存在する12B Model IDへ割り当てる場合も、Target値の変更だけで併用できる。

| suffix | 必須性 | 所有者 |
|---|---|---|
| なし | required／degradable Targetは必須、optionalは任意 | Core parser |
| `_OPTIONS_JSON` | 任意 | 選択Adapterが厳格検証 |
| `_MAX_INPUT_TOKENS` | 設定済みTargetで必須 | Core |
| `_MAX_OUTPUT_TOKENS` | 生成系Targetで必須 | CoreからProviderへ適用 |
| `_TIMEOUT_SECONDS` | 任意。Core既定値あり | Core Router |
| `_MAX_CONCURRENCY` | 任意。Core既定値あり | Core Router |

未知Target token／suffix、正でない数値、不正JSON、Adapterが認めないOption、Options内の認証情報は
起動時エラーにする。`OPTIONS_JSON`に共通timeout、concurrency、token上限を混在させない。

### 4. Capabilityごとに契約を分離する

単一の巨大な`infer()`を作らず、`generate_text`、`stream_text`、`generate_structured`、`embed`、
`estimate_input_tokens`を独立した契約にする。

Capabilityは、Adapter実装、起動時に課金なしで確認可能なModel情報、real-service conformance testの
3段階で検証する。Model固有Capabilityを起動時に証明できない場合、Adapterが要求Capabilityを実装して
いれば起動を許可し、healthへ`verification=unverified`を出してwarningを記録する。実行時の非対応は
`unsupported_capability`へ正規化し、fallbackしない。

構造化生成ではCallerがJSON Schemaを渡し、Providerへ適用した後にCoreでも再検証する。Adapterは
Privacy／Memory等のdomain型を知らない。不正JSONまたはSchema不一致をInference層で修復、再生成せず
`invalid_response`として返す。

### 5. TokenEstimateとInferenceUsageを分離する

事前判定は`TokenEstimate(count, accuracy=exact|estimated, method)`、実行後は
`InferenceUsage(input, output, total, provider_reported)`で表す。

事前推定はsystem／developer instruction、会話履歴、RAG等の取得context、current user input、JSON
Schema、Adapterが追加する既知のwrapper／区切りを含む論理入力全体を対象にする。正確なTokenizerを
利用できない場合はAdapter固有の保守的な推定と安全余裕を適用し、各Adapterが根拠とfixtureを持つ。

安全余裕込みの値を`MAX_INPUT_TOKENS`と比較し、`MAX_OUTPUT_TOKENS`を別上限としてProviderへ渡す。
Model context windowを取得できる場合は入力推定値と出力上限の合計も検証する。ProviderまたはCodex
runtime内部の不可視promptは事前上限の対象外とし、取得可能なら事後usageにだけ反映する。実usageが
推定値を超えた事実を本文なしのmetadataとして観測する。

### 6. Provider IDと認証方式を分離する

#### `ollama`

- local Providerとして固定分類する。
- endpointは既存の`OLLAMA_BASE_URL`で環境ごとに指定する。
- 既存ClientをAdapter内部で再利用できるが、domain logicから直接参照しない。

#### `openai-api`

- cloud Providerとして固定分類する。
- OpenAI API／SDKとBackend専用の`OPENAI_API_KEY`を使用する。
- ChatGPTサブスクリプションとは別のAPI課金として扱う。
- MVPは公式OpenAI API endpointへ固定し、endpointをenvで上書きさせない。
- 互換Gateway、Azure OpenAI、独自Proxyは将来の別Provider ID／Adapterとして追加する。

#### `openai-codex`

- cloud Providerとして固定分類する。
- 公式Codex runtimeへChatGPT sign-in、login cache、token refreshを委譲する。
- OAuth tokenや`auth.json`をCoreが解析、複製、log出力しない。
- stateless one-shotのtext generationだけを提供する。
- streaming、structured generation、Embedding、tool callingをCapabilityとして宣言しない。

OpenAIの公式Authentication文書は、Codex local clientがChatGPT sign-inによるsubscription accessと
API keyによるusage-based accessを別方式として扱い、ChatGPT sessionのtokenをCodex自身が更新すると
説明している。この境界に従い、2方式を単一Provider設定へ混在させない。

`openai-codex`は、allowlistした環境変数、専用Codex設定／認証directory、空の一時directory、shell／
Web search／MCP／Skills／Hooks無効化、read-only sandbox、approval禁止、strict config、timeoutを必須にする。
promptはstdin、出力はfinal textだけとし、sessionを継続しない。local file、環境変数、Web、MCPへの
アクセス不能をnegative testで確認し、禁止を保証できないCLI versionでは利用不可にする。

専用OS user、Container、network allowlistはMVP必須とせず、上記の無効化を保証できない場合または入力の
機密度を上げる場合の追加対策とする。dogfoodでは複数service共用envへOpenAI credentialを置かず、
Backendだけが読めるsecret fileまたはCodex credential領域へ分離する。

### 7. Privacy Targetだけcloudを固定拒否する

MVPでは入力単位の汎用cloud egress gateを実装しない。`privacy`はlocal Provider専用とし、cloud割当てを
起動時設定エラーにする。この制約はenvで解除できない。その他のTargetへcloud Providerを割り当てた場合は
起動時に毎回Target／Provider／Modelだけを含むwarningを出し、`ALLOW_CLOUD_INFERENCE`等の承認flagは
設けない。

Privacy classifierの障害、不正出力、未知カテゴリは既存どおり`ABSTAIN`として保存を許可しない。本ADRは
Wave 1 scanner、履歴sanitizer、RagAdmissionEvaluator等の既存privacy不変条件を緩和しない。

### 8. Inference層は1回だけ送信する

Provider固有例外を`authentication_failed`、`permission_denied`、`model_not_found`、
`unsupported_capability`、`rate_limited`、`timeout`、`unavailable`、`invalid_response`、`cancelled`、
`provider_error`、`access_denied`へ正規化する。Caller判断用の`retryable` hintを持てるが、Inference層は
retryまたは別Providerへのfallbackを行わない。SDK内部retryは無効化するか外部request回数として観測する。

| Caller | 失敗時方針 |
|---|---|
| Chat | 利用者へ共通エラーを通知 |
| Privacy | `ABSTAIN` |
| Memory extraction | worker側retry |
| Memory consolidation | `NOOP` |
| Embedding／Index | Index処理側retry |

Streaming途中失敗では送信済みtextを破棄、再送せず、部分応答を保持して中断を通知する。

### 9. Principal単位でProvider送信前に認可する

```text
InferencePrincipal
  kind: core | addon
  id: <core-purpose> | <addon-id>

authorize(principal, target) -> allow | deny
```

Core機能はコード内固定表でTargetを制限し、Addonは既定denyとする。#104がCore承認済みTarget allowlistを
渡し、Addon Manifestの自己申告だけでは許可しない。Provider送信前に認可して拒否を`access_denied`とする。
#105はCore内部Inference契約とauthorization境界、#104はAddon向けpublic／network contractを所有する。

### 10. 起動時検証とruntime状態を分離する

起動時に全Targetの形式、Provider、Model、Options、token上限、Capability適合を検証し、設定済みTargetは
課金なしの範囲でProbeする。Ollamaはendpoint／Model、OpenAI APIはcredential／endpoint到達性を確認可能な
範囲、Codexはruntimeと`codex login status`を確認する。起動時にsample inferenceは送信しない。

`chat`の接続不能はBackend起動失敗、その他の非optional Targetはwarningと`degraded`とする。定期Probeは
MVPで行わず、起動時と実request結果で状態を更新する。状態は`ready`、`degraded`、`unconfigured`、
`invalid`、検証度は`verified`、`unverified`を区別する。

- `/health/ready`: aggregateした状態とHTTP 200／503だけを返す。
- `/health/inference`: Target、状態、検証度、要求Capability、共通error category、最終確認時刻だけを返す。
- Provider／Model、endpoint、auth詳細、raw error、secret、設定値はHealth APIへ返さない。

### 11. Observabilityへpayloadを記録しない

Inference専用SQLite ledgerは追加せず、request ID、Principal、Target、Capability、Provider、Model、auth kind、
latency、TokenEstimate／InferenceUsage、外部request回数、success／error categoryを構造化logへ記録する。
prompt／response本文、secret、authorization header、raw Provider error、認証cache pathは記録しない。

### 12. Embedding変更時は別Indexへ再構築する

Chroma Index metadataへProvider ID、Model ID、Embedding dimensionからなるfingerprintを保存する。不一致の
既存Indexを検索へ使用せず、新Indexを別領域へ構築する。再Index完了までは旧Indexを維持し、完了後に
新Indexへ明示切替する。旧Indexは自動削除しない。未完了／失敗時は`reindex_required`とし、不整合な検索
結果を返さない。SQLite正本とtransactional outboxの既存契約は維持する。

### 13. 旧env互換を同一Epic内で撤去する

| 指定状態 | 移行中の動作 |
|---|---|
| 新設定のみ | 新Target設定を使用 |
| 旧設定のみ | 一時変換し、deprecated warning |
| 新旧両方 | 値が同じでも起動時エラー |
| 必須Targetが新旧どちらにもない | 起動時エラー |

Issue #181でfallback code、test、文書と`OLLAMA_CHAT_MODEL`、`OLLAMA_CLASSIFIER_MODEL`、
`OLLAMA_EXTRACTOR_MODEL`、`OLLAMA_EMBEDDING_MODEL`、`OLLAMA_CONTEXT_TOKENS`、
`OLLAMA_RESPONSE_RESERVE_TOKENS`を削除する。`OLLAMA_BASE_URL`は維持する。撤去後は旧変数の指定を
起動時エラーにし、CIでcode、設定、現行文書への再導入を検知する。

### 14. real-service証跡はIssue #181を正本にする

通常CIではcredential／serviceがないreal-service testをskip可能とする。#105完了前にはOllamaの各対応
Capability、`openai-api`のtext／structured generation、`openai-codex`のstateless text generation、
対応ProviderごとのEmbeddingを実サービスで確認する。

#181のIssueコメントを正本とし、実行時刻、commit SHA、環境区分、Provider／Model、Capability、成否だけを
記録する。CI artifactとlocal logは補助扱いとし、prompt／response、credential、endpoint／hostname、
raw error、個人情報を含む入力を記録しない。

## 却下した案

- 独立Inference Profile Registry: Targetとほぼ1対1になり三重管理となる。
- 具象Modelを含む共通`inference.yaml`: インフラ依存の環境差分とsecret境界を正本化できない。
- envによるAdapter／Capability／cloud区分の自己申告: 安全制約を設定だけで緩和できる。
- 起動時sample inference: 課金、副作用、起動遅延、非決定性をreadinessへ混ぜる。
- 自動Provider fallback: 送信先、課金、privacy、品質、重複応答を暗黙に変更する。

## 影響・トレードオフ

- Model変更はenvだけで行えるが、新Target／Adapter追加はcode変更を必要とする。
- Model Capabilityを起動時に完全証明できない場合があり、real-service testとruntime errorが必要になる。
- `privacy`以外のcloud利用はwarningだけのため、運用者が送信対象を理解して選択する必要がある。
- Inference層でretryしないため、Callerごとのfail-safeとretry budgetを維持する必要がある。
- Embedding変更では再Index用の一時容量と明示的な切替工程が必要になる。
- `openai-codex`は公式runtime versionに依存し、隔離条件を満たさないversionでは利用できない。

## 実装順

```text
#188 ADR
  -> #177 Core契約・Registry
       ├─> #178 Ollama・Chat／Privacy -> #180 Memory／Embedding ─┐
       └─> #179 OpenAI API／Codex ─────────────────────────────┼─> #181
```

`docs/system-architecture.md`、`docs/infrastructure-policy.md`、`docs/development-environment.md`、
`docs/testing-policy.md`、dogfood手順、env exampleは実装と現行運用を一致させるIssue #181で更新する。

## 既存ADRとの関係

- `ollama-model-2026-06.md`: Model検証記録とWhisper設定は維持する。Ollama用途別Model／context envは
  本ADRが置き換え、#181まで移行元の記録として残す。
- `wave2-memory-formation-retrieval-2026-08.md`: Privacy、保存、SQLite正本、Chroma派生Index、fail-safeを
  維持する。Ollama固定経路、consolidation Model固定、Embedding fingerprint／切替は本ADRを優先する。
- `local-dogfood-environment-2026-08.md`: 環境／data／process分離を維持する。OpenAI credentialを共用envへ
  置かない境界とBackend専用配置を追加適用する。
- `voice-session-contract-2026-08.md`: Streaming deltaと中断履歴のCore契約を維持し、Provider Adapterの
  error／cancel／partial response意味論だけ本ADRを追加適用する。
- `rag-memory-privacy-policy-2026-07.md`: privacy不変条件を全面的に維持する。

## 関連

- GitHub Issue #105 — Inference Provider基盤Epic
- GitHub Issue #188 — 本ADRの実装
- GitHub Issue #104 — Addon public／network contract
- GitHub Issue #58 — Development Observer
- [OpenAI Authentication](https://developers.openai.com/codex/auth)
- [OpenAI Codex CLI reference](https://developers.openai.com/codex/cli/reference)
- [OpenAI Codex Configuration reference](https://developers.openai.com/codex/config-reference)
