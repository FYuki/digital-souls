# Wave 2 記憶形成・記録分離・検索方針 (2026-08)

## 状態

**ACTIVE**。

本ADRは、Wave 2で実装する人格記憶のモデリング、保存判定、非同期記憶形成、検索順位、
domain recordとの分離を定める。

RAG privacyの絶対禁止、SQLite正本、Chroma派生index、transactional outbox等の不変条件は
`rag-memory-privacy-policy-2026-07.md`を引き続き適用する。ただし、同ADRにある次の決定は
本ADRで改定する。

- 自動抽出候補への個別同意確認と`PENDING_CONFIRMATION`
- 農業日誌、レシピ等を人格の長期記憶型として扱う旧allowlist
- 「履歴に残さないで」を履歴だけの拒否として扱うstorage scope
- Wave 2の意味分類器、記憶形成、検索順位に関するMVPの具体方式

上記の範囲で記述が競合する場合は、本ADRを優先する。

開発とdogfoodの実行境界、data root、backup、migration、rollbackは
`local-dogfood-environment-2026-08.md`を正本とする。Wave 2のSQLite／Chroma実装は同ADRの
環境identityとデータ保持契約を前提にする。

## 背景

既存文書では、RAGの検索方式、人格が保持する記憶、農業日誌等の正確な記録が「長期記憶」として
一括りになっていた。しかし、これらはライフサイクル、正確性、所有者が異なる。

- RAGは情報を検索する仕組みであり、記憶そのものの分類ではない
- 人格記憶は、ユーザーとの関係や継続的な応答に必要な経験・事実である
- 農業日誌やレシピは、正確な値とdomain固有の更新規則を持つ記録である
- 会話履歴は同一conversationを継続するための記録であり、別conversationから検索しない
- Character Card、system prompt、code、skillは人格の手続き・規則であり、会話から学習する記憶ではない

また、保存候補ごとの確認と通知は、allowlistで保存範囲を十分に限定するMVPでは操作負荷になる。
一方で、機微情報を誤って保存しないこと、ユーザーが記憶一覧から訂正・物理削除できることは
維持する必要がある。

会話由来の情報は、利用者から見た短期記憶であるconversation historyを経て、必要な内容だけが
長期記憶であるpersona memoryへ形成されるものとする。

```text
current turn
  -> conversation history（短期記憶）
       -> approved persona memory / RAG（長期記憶）
```

したがって、conversation由来の長期記憶だけを保存し、元の短期記憶を保存しない経路は作らない。
これは保存判定時の形成条件であり、後日ユーザーが会話履歴を削除した場合に、既存の長期記憶を
暗黙に連動削除することまでは意味しない。履歴と長期記憶の削除は管理画面上の別操作とする。

## 決定事項

### 1. 記憶、履歴、記録、検索を分離する

MVPでは次の概念を区別する。

| 概念 | 用途 | 永続化・取得 |
|---|---|---|
| working memory | 現在の入力、現在のgoal、処理中の文脈 | prompt/runtime内だけ。独立した長期保存tableを作らない |
| conversation history | 同一conversationの再開と表示 | SQLite。別conversationからRAG検索しない |
| persona memory | 人格が保持する経験と安定した関係情報 | `approved_memories`をSQLite正本、Chromaを派生indexとする |
| domain record | 農業日誌、レシピ等の正確な記録 | 暫定providerまたは将来のaddon DBで管理する |
| procedural knowledge | Character Card、system prompt、code、skill | 通常のpersona memoryへ保存しない |
| prospective task | 将来実行する予定、reminder | scheduler／task addonで管理し、persona memoryへ保存しない |

promptへ情報を供給する境界は、実装詳細としてのRAGに直接依存させず、概念上は次の
`ContextProvider`へ分ける。

```text
ContextProvider
├─ ConversationHistoryProvider
├─ PersonaMemoryProvider
└─ AddonRecordProvider
```

promptへ渡す項目にも履歴、人格記憶、domain recordの種別を付ける。正確な数量・日時等は
domain recordを根拠とし、人格記憶を正確な台帳として扱わない。

判断理由は、同じ検索機構で取得できることと、同じ意味・更新規則を持つことは別だからである。
会話継続、関係上の経験、正確なdomain記録を同じ型へ入れると、訂正、削除、失効、addon移行の
責任範囲が曖昧になる。取得方法よりも、情報の所有者とライフサイクルで境界を決める。

### 2. 人格記憶はepisodicとsemanticに限定する

CoALA等の認知科学由来の分類は概念整理に利用するが、MVPの永続化schemaへすべての分類を
そのまま持ち込まない。

`approved_memories.memory_kind`は次の2種類とする。

- `EPISODIC`: 時刻と文脈を伴う、ユーザーと人格の間で意味のある出来事
- `SEMANTIC`: 複数conversationでも利用する、安定したユーザーまたは対話上の事実

MVPのpositive allowlistである`memory_type`は次に限定する。

| `memory_type` | `memory_kind` | 内容 |
|---|---|---|
| `EPISODIC_EVENT` | `EPISODIC` | 共有した節目、達成、決定、結果、関係上重要な変化 |
| `USER_PREFERENCE` | `SEMANTIC` | ユーザー本人の安全な好み |
| `INTERACTION_PREFERENCE` | `SEMANTIC` | 呼び方、回答形式等の安全な対話上の希望 |

`EPISODIC_EVENT`の初期event typeは次に限定する。

- `SHARED_MILESTONE`
- `ACHIEVEMENT`
- `DECISION`
- `OUTCOME`
- `CHANGE`

`structured_value`は、`EPISODIC_EVENT`では`event_type`、`subject`、`topic`、
`USER_PREFERENCE`では`polarity`、`object`、必要な場合の`alternative`、
`INTERACTION_PREFERENCE`では`aspect`、`value`だけを持つ。`EPISODIC_EVENT.subject`は
`USER`と`SHARED`に限定し、`THIRD_PARTY`は設けない。`OUTCOME`と`CHANGE`を含む
すべてのevent typeを`topic`単一スロットで表現し、`outcome`スロットは設けない。

構造化enumの追加は文の骨格が変わる場合に限り、追加時は本ADRを改訂する。

`reflection`は記憶種別ではなく形成方法、`autobiographical`は必要になった場合のscope／subtypeとして
扱う。意味分類できない内容を受け入れる`GENERAL_MEMORY`は作らない。

判断理由は、研究上の分類をそのまま単一tableの列挙へ変換すると、検索対象でない手続きやtaskまで
persona memoryへ混在するためである。MVPは、関係の連続性に直接必要な出来事と安定した事実だけを
狭いallowlistにし、新しい型は保存policyと管理方法を定義できた時点で追加する。

### 3. domain recordを人格記憶から分離する

`provider_id`はデータの所有境界を表し、記憶の意味分類には使用しない。

MVPでは次の値から開始する。

```text
core
temporary:agriculture
temporary:recipe
```

- `approved_memories`は人格側が所有する記憶だけを持ち、`provider_id=core`とする
- 農業日誌とレシピは別の`temporary_provider_records`で保持する
- addon完成後は、`temporary:*`のrecordを対応するaddon DBへ移行し、暫定recordを物理削除する
- addonはpersona memory DBへ直接書き込まず、型付き`DomainEvent`を発行する
- persona memoryへ形成したepisodeは、元recordの移行後も`provider_id=core`のまま残す
- provenanceには元の`source_provider_id`とsource IDを保持する

すべてのdomain recordをepisodeへ変換しない。共有した節目、達成、決定、結果、変化のうち、
将来の関係的な応答に有用でprivacy検査を通過したものだけを`EPISODIC_EVENT`候補にする。

例えば「トマトを12.4kg収穫した」という正確な数量は農業recordに保持し、persona memoryには
「ユーザーと今季最初のトマト収穫を迎え、一緒に成果を喜んだ」のような関係上の出来事を保持する。

判断理由は、domain recordには正確な訂正、集計、schema migrationが必要なのに対し、persona memoryは
その出来事が関係にとって何を意味したかを保持するためである。addon完成後に記録の所有先を移しても、
人格側の経験まで失わないよう、所有者とprovenanceを分ける。

### 4. SQLite正本のschemaは用途を明示する

persona memory系の`approved_memories`、`memory_sources`、`memory_lineage`、
`memory_write_receipts`、`memory_index_outbox`、`temporary_provider_records`は、
専用の`persona-memory.db`に配置する。
`conversation-history.db`とはファイルを分け、会話履歴schemaの変更やpersona memory側の
ロールバックによる履歴の巻き戻しを発生させない。memory行とoutbox行は同一ファイル内で
更新し、トランザクション原子性を保つ。

`approved_memories`は少なくとも次を持つ。

```text
id: UUID text
character_id
provider_id: core
memory_kind: EPISODIC / SEMANTIC
memory_type
episodic_event_type: nullable
formation_method: DIRECT / EXTRACTED / ADDON_EVENT / CONSOLIDATED
schema_version
normalized_text
structured_value
policy_version
classifier_version
model_id
model_digest
prompt_version
content_version
status: ACTIVE / INACTIVE
idempotency_key: character_id単位で一意
last_write_idempotency_key: nullable
occurred_at: nullable
occurred_timezone: nullable
occurred_precision: nullable
stated_at
expires_at: nullable
last_user_mentioned_at: nullable
last_consolidated_at: nullable
created_at
updated_at
```

`created_at`はSQLite登録日（有効化日）、`occurred_at`は出来事が発生した時刻、`stated_at`は
根拠となった発言turnの時刻、`last_user_mentioned_at`はユーザーがその内容を最後に明示言及した
時刻であり、それぞれ別の意味を持つ。出来事の日付が不明な場合は`occurred_at`、
`occurred_timezone`、`occurred_precision`をすべて`NULL`にし、`stated_at`では補完しない。
出来事日が既知の場合は3列をすべて設定する。`stated_at`は常にtimezone-awareな値を保存する。

Issue #11でpersona memory schemaをversion 2へ更新する。version 1からのmigrationは実装せず、
既存dogfoodのpersona memory DBは再作成する。`temporary_provider_records`と将来のaddon recordは
この時系列照合の対象外とし、同tableの`effective_at`契約は変更しない。

provenanceは自由形式JSONだけに埋めず、型付き`memory_sources`で会話turn、addon event、
provider record等との関係を保持する。複数のsourceから形成される記憶に対応するため、
単一の`parent_id`や`consolidated` boolだけに依存しない。

記憶同士の系譜は多対多の`memory_lineage`で保持し、関係を次に限定する。

- `CONSOLIDATED_FROM`
- `SUPERSEDES`
- `DUPLICATE_OF`

Issue #8では、永続化用の`MemoryWriteContext`が型付きsource識別子と任意のlineageを受け取る。
`save`／`correct`は`approved_memories`、`memory_sources`、`memory_lineage`、
`memory_write_receipts`、`memory_index_outbox`を同一トランザクションで更新する。
候補抽出（#10）、runtime統合（#29）、nightly consolidation（#48）はこの境界へ入力を供給するが、
provenanceとlineageの永続化経路そのものはIssue #8で提供する。

`memory_write_receipts`は初回保存と訂正の冪等性キーを`character_id`単位で不変記録する。
これにより、後続の訂正後に過去の訂正が再試行されても、本文の再更新とoutboxの重複作成を防ぐ。

ChromaにはSQLiteの`memory_id`、`normalized_text`、embeddingと検索filterに必要な最小metadataだけを
mirrorする。`last_user_mentioned_at`だけが変わる場合、Chromaは更新しない。

MVPでは次をschemaへ追加しない。

- LLMが推定した連続値の`importance`、`confidence`
- ユーザーの心理状態を推定する`emotional_valence`、`emotional_intensity`
- `access_count`、`last_accessed_at`
- 自由形式のassociation graph、entity graph
- 会話から学習した記憶を常時挿入する`pinned`

判断理由は、SQLiteを監査・訂正・削除の正本、Chromaを再構築可能なindexとして維持するためである。
出所とversionは後から判定を説明できる形で保持する一方、根拠の弱いLLM推定値や検索回数由来の値を
正本へ入れず、MVPの更新規則を決定的に保つ。

### 5. allowlistを保存同意として扱い、個別確認・通知を行わない

MVPでは、次の条件をすべて満たす候補を自動保存する。

```text
positive allowlistの型
+ privacy検査通過
+ current turnに保存拒否指示なし
+ conversation由来の場合はsource turnの履歴本文が保存済み
= 保存同意済みのApprovedMemoryCandidate
```

候補ごとのユーザー確認、保存通知、確認待ち候補の永続化、確認TTLは実装しない。
`PENDING_CONFIRMATION`も保存判定から削除する。

ユーザーの「覚えて」は候補抽出を促すが、絶対禁止、意味分類、allowlistを迂回しない。
保存内容は記憶一覧UIから閲覧、訂正、物理削除できるようにする。

判断理由は、allowlistを狭く固定し、privacy判定をfail-closedにし、保存後の管理手段を用意すれば、
候補ごとの確認で会話を中断する負担を避けられるためである。保存通知も同じ情報を重ねて提示するだけに
なるためMVPでは行わず、ユーザーの制御は記憶一覧で一元化する。

### 6. 保存拒否指示はcurrent turnの履歴と記憶形成へ適用する

用語上の指示は次のscopeとする。

| 指示 | scope | 処置 |
|---|---|---|
| 「覚えないで」「記憶しないで」 | `RAG` | persona memory／RAGへ保存しない。履歴は通常policyに従う |
| 「履歴に残さないで」 | `BOTH` | 履歴本文、persona memory／RAG、current turn由来の暫定domain recordへ保存しない |
| 「保存しないで」「記録しないで」 | `BOTH` | 履歴本文、persona memory／RAG、current turn由来の暫定domain recordへ保存しない |

MVPの`StorageScope`は`RAG`と`BOTH`に限定し、`HISTORY`は削除する。効力はcurrent userの
current turnだけとする。保存拒否があるturnでは、既存記憶への`last_user_mentioned_at`更新も
行わない。

conversation由来の記憶形成には、元turnの履歴本文が保存済みであることを必須条件とする。

```text
conversation-derived ALLOW_STRUCTURED
  => source turnがcompleted
  AND source turnの履歴本文が保存済み
```

履歴用sanitizerが`SKIP_CONTENT`を返した場合、turnが`privacy_skipped`になった場合、または
ユーザーが履歴保存を拒否した場合は、候補抽出、`TOUCH`、暫定domain record作成を含む後続の
永続化を行わない。assistant側の検査結果でturn全体の履歴本文を消去した場合も同様とする。

判断理由は、短期記憶から長期記憶を形成するモデルでは、元の短期記憶が存在しないのに長期記憶だけが
残る状態はユーザーの保存意図と一致しないためである。このため`HISTORY`だけを拒否してRAGを許可する
scopeは設けない。一方、「覚えないで」は履歴を残しつつ長期化だけを止める要求として有効なため、
`RAG` scopeは残す。addon自身のrecordから発生する`DomainEvent`はconversation由来ではないため、
この形成条件ではなくaddon側の保存policyに従う。

### 7. classifierとadmission evaluatorの責務を分離する

Wave 1の決定論的privacy scanner、Wave 2の`SemanticPrivacyClassifier`、
`RagAdmissionEvaluator`は別の責務を持つ。

| component | 責務 | 行わないこと |
|---|---|---|
| Wave 1 privacy scanner | 秘密値、直接識別値、保存拒否指示を決定論的に検出する | 文脈依存の意味分類、保存先の最終決定 |
| `SemanticPrivacyClassifier` | health、心理状態、第三者情報、暗示表現等を文脈から分類する | allowlist変更、保存可否・権限の決定 |
| `RagAdmissionEvaluator` | scanner finding、semantic assessment、候補型から最終結果を決定論的に算出する | LLM呼び出し、DB／embedding／Chroma操作 |

MVPでは`SemanticSignalScreener`やkeywordによる意味的な前段filterを設けず、allowlist候補を
すべて`SemanticPrivacyClassifier`へ渡す。暗示表現がclassifierへ届かない経路を作らない。
最適化が必要になった場合はencoderによる前段実装を検討するが、Wave 1 scannerは引き続き
絶対禁止と明示的保存拒否の境界として残す。

Wave 1 scannerが絶対禁止または保存拒否を確定した場合はclassifierを呼ばず、直ちに拒否する。
classifierのtimeout、モデル未ロード、未知言語、不正出力、未知カテゴリは安全側の
`ABSTAIN_UNKNOWN`とし、保存しない。

保存判定は次の5状態とする。

| 状態 | 意味 | 永続化副作用 |
|---|---|---|
| `DENY_SENSITIVE` | 機微情報または絶対禁止 | なし |
| `DENY_USER_REQUEST` | current turnの保存拒否 | なし |
| `ABSTAIN_UNKNOWN` | 判定不能またはclassifier障害 | なし |
| `NOT_MEMORY_WORTHY` | 安全だがallowlist外または長期的価値なし | なし |
| `ALLOW_STRUCTURED` | allowlistへ正規化されprivacy検査通過 | `ApprovedMemoryCandidate`を生成可能 |

`ALLOW_STRUCTURED`だけがrepositoryへ到達できる。LLMは`character_id`、`provider_id`、保存先、
allowlistを選択しない。

判断理由は、意味の理解には文脈を扱えるclassifierが必要だが、非決定的なmodelへ保存権限まで
与えると、model、prompt、障害時fallbackの変更でpolicyが変わるためである。また、意味的な
前段filterを置くと、そこで取りこぼした暗示表現がclassifierへ到達しない。絶対禁止は安価で
決定的なscannerで即時終了し、それ以外の候補はclassifierへ渡した後、applicationが最終決定する。

### 8. 意味分類器はlocal LLMで開始し、versionを分離して記録する

MVPの`SemanticPrivacyClassifier`はローカルOllamaの`gemma4:e4b`を会話モデルと共有して開始する。
分類器が会話と競合して応答遅延やtimeoutを発生させることが観測された場合は、会話処理を優先し、
必要に応じて分類器専用model instanceへ分離する。

判定のprovenanceは結合文字列にせず、次の独立したfieldで表現する。

```text
classifier_version
model_id
model_digest
prompt_version
policy_version
```

Wave 1 scannerの結果だけで終了しclassifierを呼ばなかった場合、semantic provenanceは`NULL`とする。
本Wave 2契約のpolicy versionは`2026-08-wave2-v1`とし、`HISTORY` scopeの削除と
同じバージョンで管理する。

判断理由は、機微な本文を外部APIへ送らず、既存の常用modelを流用してMVPの構成とmemory使用量を
小さくするためである。最初からinstanceを分けず、会話との競合をlatencyとtimeoutで観測してから
分離する。versionを独立fieldにするのは、model更新、prompt変更、classifier実装変更、policy変更の
どれが判定差を生んだかを後から切り分けるためである。

### 9. 会話応答と記憶形成を分離する

会話応答は記憶形成の完了を待たない。同期経路は概ね次とする。

```text
current user input
  -> Wave 1 scanner / history sanitizer
  -> 同一conversationのsanitized history取得
  -> current queryのRAG検索可否判定
  -> 許可された場合だけpersona memory検索
  -> PromptBuilderで型付きcontextを合成
  -> conversation LLM
  -> assistant応答のscanner / sanitizer
  -> conversation turnを完了
  -> userへ応答
```

現在のuser原文は応答生成中だけ使用できるが、原文、原文hash、マスク前本文をSQLite、log、例外へ
残さない。userとassistantの双方に同じ履歴用scanner／sanitizerを適用する。

非同期の記憶形成は概ね次とする。

```text
completedかつ履歴本文保存済みのsanitized source turn
  -> Wave 1 finding、保存拒否、絶対禁止を確認
  -> allowlist候補を構造化抽出
  -> candidate schemaを決定論的に検証
  -> candidateへWave 1 scannerを再適用
  -> sourceとcandidateをSemanticPrivacyClassifierで評価
  -> RagAdmissionEvaluator
  -> ALLOW_STRUCTUREDだけSQLite memory + UPSERT outboxを同一transactionで保存
  -> workerがembeddingを生成してChromaへupsert
```

候補抽出器はclassifierとは別componentとする。現在user turnと、省略表現の解決に必要な直近の
sanitized turnだけをsourceにし、RAG検索結果を新規候補の根拠にしない。出力は型付きschema、
最大3候補、決定的な生成設定から開始し、`normalized_text`はIssue #33の
admissionが構造化値から決定的に生成する。候補抽出器や永続化層は生成しない。
確認待ち候補や生source本文は永続化しない。

Issue #11以降、候補抽出器は相対・絶対・複数の日付表現だけを型付きで返し、絶対日時への変換は
行わない。applicationが`stated_at`を基準点、起動時に解決したtimezoneを基準として、月末、
年末、閏年、年跨ぎを含め決定論的に`occurred_*`へ解決する。日付表現がない、または解決不能な
場合は`occurred_*`を3列とも`NULL`にする。

非同期classifierはbounded retryを行う。初期値は1回15秒、最大2回、全体35秒以内、queue滞留
5分以内とし、設定で変更可能にする。上限を超えた候補は`ABSTAIN_UNKNOWN`として破棄する。

conversation由来のjobは、source turnが`completed`になり履歴本文の保存が確定してから投入する。
形成処理中にsource turnが存在しない、本文非保存、`privacy_skipped`へ変化していることを検出した
場合も、副作用なしで終了する。

判断理由は、会話応答がclassifier、embedding、Chroma登録、retryを待つと体感速度と可用性を
損なうためである。ただし非同期化によって「履歴なし・長期記憶だけ」の経路を作らないよう、
source turnの状態をjob投入時とadmission時に確認する。

### 10. 機微なcurrent queryではRAG検索を行わない

現在のuser発言が機微である場合、保存済み記憶の検索自体をskipする。ローカルembeddingだけを
許可して検索結果を再検証する方式は採用しない。

- Wave 1の絶対禁止findingまたは`ScanFailure`: RAG検索をskip
- semantic assessmentが`SENSITIVE`または`ABSTAIN`: RAG検索をskip
- semantic assessmentが`NOT_SENSITIVE`: RAG検索を許可

検索前classifierは同期経路のため、短いtimeoutで1回だけ呼び、retryしない。初期timeoutは3秒とし、
timeout、不正出力、モデル未ロード時はRAG検索だけをskipして通常会話を続ける。得られたassessmentは、
同じsourceに対する非同期admissionで安全に再利用できる場合は再利用してよい。

判断理由は、機微queryをembeddingしてindexへ問い合わせた時点で、検索後の再検証より前に
機微な意図と既存記憶の関連付けが発生するためである。検索結果だけを厳しく検査する方式では
この境界を守れないため、判定不能を含めて検索前にskipし、会話そのものは継続する。

### 11. 検索順位は意味的関連度を主、最終言及日時をtie-breakとする

MVPの検索順位は次の原則とする。

1. Chromaの意味的関連度で候補を取得する
2. `memory_id`でSQLite正本を引き直し、`character_id`、状態、TTL、policy version、本文の
   決定論的再検査を通過した候補だけを残す
3. 関連度閾値を満たす候補を意味的関連度の降順で並べる
4. 関連度が同等とみなせる候補間だけ、`last_user_mentioned_at`の新しいものを優先する
5. さらに同順位なら`created_at DESC`、`id ASC`で安定順序を作る
6. 設定された最大件数とprompt token budgetの範囲で注入する

概念上の順序は次になる。

```text
semantic_relevance DESC
-> last_user_mentioned_at DESC NULLS LAST  # 同等関連度内だけ
-> created_at DESC
-> id ASC
```

「同等関連度」の許容幅はIssue #9の固定検索corpusで決め、設定値として持つ。最近言及されたことを
理由に、明確に関連度の低い記憶を繰り上げない。

Issue #11以降、時間条件を決定論的パーサで抽出できたqueryは、SQLiteの`occurred_at`期間検索と
Chroma意味検索を両方実行してunionする。一致種別の優先順位は、両方一致、意味一致、期間一致の
順とする。同じ一致種別では意味的関連度を主とし、意味距離を持たない期間一致同士では
`last_user_mentioned_at`をtie-breakに用いる。時間条件なし、またはパース失敗時は従来どおり
Chroma一次、SQLite再検証へ縮退する。query解析の同期経路へLLM往復は追加しない。

日時検索の判断根拠はSQLite正本だけとし、Chromaの`occurred_at` metadataはindex整合確認の補助に
限る。Chroma由来候補もSQLiteで再検証し、promptにはSQLite検証済みの`occurred_at`と精度を併記する。
出来事日不明の記憶は日時ラベルなしで提示する。両経路が正常終了して0件の場合は、該当なしと
推測禁止を明示する。検索で使用したmemory ID、出来事日時、精度、一致種別はmetadata-onlyで
追跡し、query本文とmemory本文はlogへ残さない。

季節は`occurred_at`の月から決定論的に導出し、春を3〜5月、夏を6〜8月、秋を9〜11月、冬を
12〜2月とする。冬は年を跨ぎ、「去年の冬」は前年12月から当年2月までとする。
`occurred_precision`が`YEAR`または`NULL`の記憶は季節照合からreason code付きで除外する。
`TemporalPrecision`は`YEAR`から`SECOND`までの既存6値を維持し、季節値を追加しない。

`last_user_mentioned_at`はユーザーが明示的に新規言及、再言及、訂正した場合だけ更新する。
検索、prompt注入、assistantによる言及、addon recordの更新、consolidationでは更新しない。
新しいuser由来の記憶では言及日時を設定し、addonだけから形成した記憶は`NULL`から開始する。
consolidationではsource群の最大日時を引き継ぐ。

次はMVPで採用しない。

- 検索された回数による強化
- エビングハウス型またはその他の自動減衰・忘却
- 直近性、重要度、関連度の固定重み`0.5 / 2 / 3`

判断理由は、検索の目的は現在の問いに合う記憶を選ぶことであり、単に新しい、頻繁に検索された、
LLMが重要と推定したという理由で関連性を逆転させないためである。ユーザーの再言及は明示的な
新しさのsignalとして採用するが、意味的関連度が同等の場合だけ使う。検索自体では日時を更新せず、
検索された記憶がさらに検索されやすくなるfeedback loopを作らない。

### 12. 重複は追加時に安全な範囲だけ処理し、意味的整理は後続batchに分ける

追加時は次だけを同期的に行う。

- `character_id + source_conversation_id + source_turn_id + candidate_index + extractor_version`を
  基礎にしたidempotency keyでretryによる重複を防ぐ
- 型ごとの決定論的natural keyで完全一致または明白な同一記憶を検出する
- 同一内容へのユーザーの再言及は`TOUCH`として`last_user_mentioned_at`だけを更新する

idempotency keyに生本文hashを使用しない。`TOUCH`では本文、構造化値、`content_version`、
本文変更日時としての`updated_at`を変更せず、Chroma outboxを作らない。保存拒否またはprivacy拒否の
turnでは`TOUCH`も行わない。

embedding類似度やLLMによる意味的mergeを追加経路へ入れない。会話応答と保存処理を重くし、
誤mergeを即時に確定させるためである。

意味的な重複整理が必要になった場合は、夜間またはidle時の単一background jobとして追加する。
対象は`provider_id=core`のpersona memoryだけとし、temporary／addon recordは対象外とする。
LLMは次の型付きplanだけを返し、applicationがtype、character、version、privacy、lineageを検証して
適用する。

```text
KEEP / MERGE / SUPERSEDE / DELETE_EXACT_DUPLICATE / CONFLICT / NOOP
```

曖昧な候補を自動削除しない。MVPではschemaとlineageだけ準備し、nightly consolidationの実装は
記憶登録・管理UI・outboxの完成後にIssue #48で行う。

判断理由は、retryや完全一致は追加時に安全かつ安価に判定できる一方、言い換え、矛盾、時間変化を
含む意味的重複には複数記憶をまとめて見る必要があるためである。追加時の誤mergeは直ちに記憶を
失わせるので避け、会話負荷の低い時間帯に型付きplanを検証して整理する。これは人が活動中に
覚え、休息時に記憶を整理するモデルにも対応する。

### 13. promptfooとpytestの責務を分けてclassifierを評価する

意味分類器のconformance testはpromptfooを用い、通常のunit testへ実model評価を混在させない。

```text
backend/evals/privacy_classifier/
├─ cases.jsonl
├─ prompt-lab.yaml
├─ conformance.yaml
├─ provider.py
└─ README.md
```

- `prompt-lab.yaml`: Ollamaを直接呼び、productionで観測した失敗を合成case化し、redから最小の
  prompt修正でgreenへするprompt tuning用
- `conformance.yaml`: Python providerを介してproductionの`SemanticPrivacyClassifier`、
  parser、version伝搬を含めて評価するrelease gate
- pytest unit: prompt組立、schema、parser、fail-closed、`RagAdmissionEvaluator`をfakeで検証する
- integration test: 実Ollamaへ接続する少数のsmoke caseを通常unit testと分離する

実ユーザー本文をcorpusへコピーせず、問題の構造だけを再現する合成caseを作る。機微caseと安全caseは
対になるよう用意する。構造化enumの正誤は決定論的assertionで検査し、規則で表現できる判定へ
LLM-as-judgeを使用しない。

合格基準は、安全側へ倒す。

- 固定した機微caseが`NOT_SENSITIVE`になることを許容しない
- `SENSITIVE`または`ABSTAIN`は安全側の結果として許容する
- timeout、未知言語、モデル未ロード、不正出力が保存許可にならない
- 安全なallowlist候補の過検知は記録し、prompt tuningで改善するが、機微caseを保存可能にして
  recallを上げない

重要な変更では対象suiteを3回反復してから全suiteを実行する。release時はmodel、prompt、policyの
versionを固定し、cacheを無効にした全caseの反復結果を残す。promptfooはversionを固定した
development dependencyとし、結果や本文を外部共有しない。

判断理由は、pytestだけでは非決定的な実modelの意味分類品質を評価できず、promptfooだけでは
production adapter、parser、fail-closedのcode contractを十分に保証できないためである。
productionの失敗を合成caseへ変換し、red、最小修正、green、regressionの順で残すことで、
実ユーザー本文を保存せずにprompt改善の履歴を作る。

### 14. 会話品質への影響を観測してから最適化する

非同期admission workerのconcurrencyは1から開始し、会話処理を優先する。少なくとも次を
metadata-onlyで観測する。

- 会話応答のp95 latency
- classifierのqueue待ち時間とtimeout率
- model待ち時間
- GPU／memory使用量
- admission decision、reason code、model／prompt／policy version

会話遅延、classifier timeout、queue滞留が継続的に発生した場合、専用instanceまたはencoderによる
前段最適化を検討する。固定検索重み、access強化、減衰、semantic screenerは、運用上の問題が
観測される前には追加しない。

判断理由は、MVP時点では競合の有無と支配的なbottleneckが分からず、先に専用instance、encoder、
複合rankingを入れると構成と評価軸だけが増えるためである。会話p95、queue待ち、timeout等の
観測結果から、必要な最適化だけを選ぶ。

### 15. Wave 2はprivacy判定から検索、形成、管理の順で進める

実装順は親Issue #28で管理し、次の依存順とする。

```text
#25（Wave 1 privacy scanner、完了）
  -> #50（dogfood環境分離、完了）
  -> #22
  -> #33
  -> #8
  -> (#29 || #30)
  -> #31
  -> #9
  -> #10
  -> (#11 || #12)
  -> #28の受入確認
```

判断理由は、#22の再開前に、dev／testとdogfoodのdata root、port、process ownership、
backup／migration境界を固定し、TAKTからdogfoodデータへ到達できないことを#50で受け入れる
必要があったためである。#50は2026-08-17に手動受入まで完了した。
その後は意味分類と決定論的admissionを確立し、SQLite正本、Chroma同期、検索を構築する。
自動記憶形成は安全な保存境界と取得境界が完成してから接続する。時系列照合と管理UIは、共通の
schemaとrepositoryが完成した後で並行して進めてよい。nightly consolidationはこの依存列の
完了後に別Issueとして追加する。

### 16. core persona memoryをidle時に原子的にconsolidationする

Issue #48では、`provider_id=core`のactive persona memoryだけをcharacterとmemory typeごとに
分離し、夜間または一定idle時間後にconcurrency 1で整理する。会話処理、非同期admission、
Chroma outboxに未処理作業がある間は起動せず、実行中に優先作業が生じた場合は次のmemory単位へ
進む前に停止する。

planは`KEEP`、`MERGE`、`SUPERSEDE`、`DELETE_EXACT_DUPLICATE`、`CONFLICT`、`NOOP`の
6種に限定する。LLMはローカルOllamaだけを使用し、applicationがcharacter、provider、memory
kind／type、content version、privacy、source、lineageをSQLite正本から再検証する。
`CONFLICT`と`NOOP`はDBを変更せず、物理削除は本文・構造化値・時間情報が完全一致する場合だけ
許可する。

`MERGE`と`SUPERSEDE`は`formation_method=CONSOLIDATED`の新規行を作り、元行を
`INACTIVE`にする。新行から元行へ`CONSOLIDATED_FROM`または`SUPERSEDES`のlineageを張り、
`memory_sources.source_type=CONSOLIDATION`、`source_provider_id=core`、元memory idの
`source_ref`を保存する。新規行、source、lineage、write receipt、旧行状態変更、新旧outboxは
単一SQLite transactionで確定する。旧行にも`UPSERT` outboxを作り、index workerがinactive行を
Chromaから削除する。

consolidationのidempotency keyは`consolidation`、character id、plan種別、
`consolidation-v1`、memory idとcontent versionを昇順に並べた値のSHA-256から構成する。
同じ入力snapshotの再実行では新規行やDELETE outboxを重複生成しない。`SCHEMA_VERSION`は2のまま
維持し、既存DBの`memory_sources` CHECK制約だけを行を保持したまま更新する。

ログは件数、plan種別、latency、reason code、model／prompt／policy versionに限定し、
CONFLICT時だけ対象memory idを記録する。memory本文、prompt、model出力全文は記録しない。

## MVPで実装しない項目

- 候補ごとの同意確認、保存通知、確認待ち状態
- `SemanticSignalScreener`または決定論的な意味keyword gate
- access countによる強化、時間減衰、自動忘却
- 固定の複合検索重み
- LLM推定の重要度、感情、心理状態を記憶metadataとして保存すること
- raw会話turnをそのままepisodic memoryとして保存すること
- persona memory、domain record、task、procedural knowledgeを1つの汎用tableへ混在させること
- 追加時のLLM／embeddingによる意味的merge
- 曖昧な記憶の自動削除
- entity graph、自由形式association graph
- addon完成前にtemporary recordをpersona memoryへ移すこと

## 影響・トレードオフ

- 個別確認なしで記憶が増えるが、保存対象は狭いallowlistとprivacy gateに限定され、記憶一覧から
  ユーザーが管理できる
- classifier障害や怪しい候補は保存されないため、記憶の取りこぼしは許容する
- 会話応答を非同期記憶形成から分離することで、登録処理のretryが通常会話を待たせない
- 機微queryでは関連記憶があっても検索しないため回答の個別性は下がるが、機微なqueryから
  記憶indexへアクセスしないことを優先する
- domain recordとpersona memoryの二重表現が発生し得るが、正確な記録と関係上の経験を混同しない
- SQLite、Chroma、将来のaddon DB間のprovenanceと同期設計が必要になる
- 最終言及日時をtie-breakだけに使うため、検索feedback loopを作らず再言及の新しさを反映できる
- 自動減衰を行わないため、忘却はユーザー削除、失効、訂正、後続consolidationで明示的に扱う

## 関連

- `docs/decisions/rag-memory-privacy-policy-2026-07.md` — privacy不変条件、SQLite／Chroma、outbox
- `docs/decisions/archive/miori-memory-policy-2026-06.md` — 本ADRへ統合済みの初期検討履歴
- `docs/decisions/archive/Multi-character-db-2026-06.md` — characterごとのデータ分離に関する初期検討履歴
- `docs/decisions/character-card-v3-prompt-builder-2026-07.md` — prompt合成境界
- `docs/system-architecture.md` — システム全体の責務分離
- `docs/testing-policy.md` — unit／integration／E2Eの区分
- GitHub Issue #22 — 意味分類器
- GitHub Issue #33 — 決定論的な長期記憶保存判定
- GitHub Issue #8 — SQLite正本とoutbox
- GitHub Issue #29、#30、#31 — 承認済み記憶と検索基盤の後続作業
- GitHub Issue #9 — 検索・再検証・順位付け
- GitHub Issue #10 — 記憶候補抽出と自動保存
- GitHub Issue #11 — 記憶の時系列照合
- GitHub Issue #12 — 記憶管理UI
- GitHub Issue #28 — Wave 2親Issueと受入確認
- GitHub Issue #48 — Wave 2完了後のidle時persona memory consolidation
- [プロンプトエンジニアリングのすすめかた](https://zenn.dev/nrs/articles/70db94beb7b76d) —
  productionの失敗を固定caseへ変換するred／green／regression運用
