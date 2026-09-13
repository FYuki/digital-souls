# Wave 2 記憶形成・記録分離・検索方針 (2026-08)

## 状態

**ACTIVE**。Wave 2の保存・privacy・検索基盤の判断を保持する。

2026-09-12の#352により、Episode / Fact / Semanticの正本、5W、日時、形成経路、Fact照合・失効は
[Episode・Fact・Semanticの正本と形成責務](episode-fact-semantic-boundaries-2026-09.md)を優先する。
本ADRの旧subject / topic単一スロット、現在+直前turnだけのEpisode抽出、意味記憶の形成方法を
新モデルの設計制約にしない。以下で「既存Wave 2」と明記した物理schema・処理は従来基盤の記録であり、
新モデルの実装完了や旧形式互換を要求する記述ではない。

Inference Provider／Target、Ollama固定経路、consolidation Modelの選択、Embedding Index fingerprintと
再構築時の切替については`inference-provider-foundation-2026-09.md`を優先する。本ADRのprivacy、
保存allowlistによる検証、fail-closed、SQLite正本、Chroma派生Index、transactional outboxは引き続き有効であり、
Provider変更によって緩和しない。

RAG privacyの絶対禁止、SQLite正本、Chroma派生index、transactional outbox等の不変条件は
`rag-memory-privacy-policy-2026-07.md`を引き続き適用する。ただし、同ADRにある次の決定は
本ADRで改定する。

- 自動抽出候補への個別同意確認と`PENDING_CONFIRMATION`
- 農業日誌、レシピ等を人格の長期記憶型として扱う旧allowlist
- 「履歴に残さないで」を履歴だけの拒否として扱うstorage scope
- Wave 2の意味分類器、記憶形成、検索順位に関するMVPの具体方式

上記の範囲では本ADRを優先し、その後のEpisode / Fact / Semantic責務再編は前述の2026-09 ADRを優先する。

開発とdogfoodの実行境界、data root、backup、migration、rollbackは
`local-dogfood-environment-2026-08.md`を正本とする。SQLite／Chroma実装は同ADRの
環境identityとデータ保持契約を前提にする。

## 背景

既存文書では、RAGの検索方式、人格が保持する記憶、農業日誌等の正確な記録が「長期記憶」として
一括りになっていた。しかし、これらはライフサイクル、正確性、所有者が異なる。

- RAGは情報を検索する仕組みであり、記憶そのものの分類ではない
- 人格記憶は、ユーザーとの関係や継続的な応答に必要な経験・事実である
- 農業日誌やレシピは、正確な値とdomain固有の更新規則を持つ記録である
- 会話履歴は同一conversationを継続するための記録であり、別conversationから検索しない
- Character Card、system prompt、code、skillは、通常のpersona memoryとは正本・更新経路を分ける

保存候補ごとの確認と通知は、allowlistで保存範囲を限定するMVPでは操作負荷になる。
一方で、機微情報を誤って保存しないこと、ユーザーが記憶一覧から訂正・物理削除できることは維持する。

会話由来の情報はconversation historyを経て、必要な内容だけをpersona memoryへ形成する。

```text
current turn
  -> 保存許可されたconversation history
       +-> Episode / Fact形成・同thread登録（#340）
       +-> Direct Semantic Extraction（#341）
```

conversation由来の長期記憶だけを保存し、元の履歴本文を保存しない経路は作らない。
後日の会話履歴削除と長期記憶の物理削除は別操作だが、参照元失効後も依存情報を有効のままにしてよいわけではない。
新モデルではsource / 版 / Fact統合関係に依存する結果を即時利用停止・再評価する。物理連動削除とは区別する。

## 決定事項

### 1. 記憶、履歴、記録、検索を分離する

次の概念を区別する。

| 概念 | 用途 | 永続化・取得 |
|---|---|---|
| working memory | 現在の入力、現在のgoal、処理中の文脈 | prompt/runtime内。独立した長期保存tableを作らない |
| conversation history | 同一conversationの再開と表示 | SQLite。別conversationからRAG検索しない |
| persona memory | キャラクターの経験と知識 | SQLite正本、Chroma派生index。Episode / Semantic / Reflectionの責務を分離 |
| Related Facts | 経験で得た対象の情報・申告内容 | #342のID付きFactとEpisode–Fact参照。外部domainやSemanticの正本ではない |
| domain record | 農業日誌、レシピ等の正確な記録 | 暫定providerまたはaddon DBで管理 |
| procedural knowledge | Character Card、system prompt、code、skill | 通常のpersona memoryへ保存しない。学習Skillは#102の別経路 |
| prospective task | 将来実行する予定、reminder | scheduler／task addonで管理。#249のLife Stateとは正本を区別 |

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

### 2. Episode・Fact・Semantic・Reflectionの正本を分ける

既存Wave 2の`approved_memories.memory_kind`はEPISODIC / SEMANTICの2種類で、positive allowlistは次のとおりである。

| 既存memory_type | memory_kind | 内容 |
|---|---|---|
| EPISODIC_EVENT | EPISODIC | 共有した節目、達成、決定、結果、関係上重要な変化 |
| USER_PREFERENCE | SEMANTIC | ユーザー本人の安全な好み |
| INTERACTION_PREFERENCE | SEMANTIC | 呼び方、回答形式等の安全な対話上の希望 |

既存の直接形成preferenceがあることと、新しい汎用Semantic schema・formation_typeが実装済みであることは別である。
新しい採用設計では次を適用する。具体的な物理schema・型の追加は#342 / #345で検証する。

- #340: Episodeは所有characterの経験。内容は5W、必須はWhatの述語のみ。他の5Wの欠損を許容する。
- owner character_idは管理境界であり、不明な行為者を所有者で補完しない。
- 旧subject=USER/SHARED/SELF、topic単一スロット、event typeの旧必須性を新形式の制約にしない。
- Related Factsは対象の5Wを持つ独立ID付きレコードとし、EpisodeからID参照する。取得元・版・経緯を維持する。
- #341: SemanticはDIRECT_EXTRACTION / EXPERIENCE_DERIVEDを形成方法として区別し、共通Storeで管理する。
- #100: EXPERIENCE_DERIVED形成とReflectionを所有する。Reflectionは単なる形成方法ではなく別の永続正本とする。
- Reflectionを通常会話のRAG / promptへ直接注入しない。Current Interests・人格等への間接反映を維持する。

2026-09-13の#340合意では、日常経験も対象に含め、特別な重要性がないことだけでは除外しない。
上表の旧EPISODIC_EVENTの節目・達成等を、新しいEpisodeの保存対象の上限にしない。
明示的な仮定・創作を話した経験とそのFactも、仮定・創作の文脈を保持して保存・検索・利用する。
一続きの経験と語り直し、Fact情報の補足・訂正は[境界ADR](episode-fact-semantic-boundaries-2026-09.md)に従う。

意味分類できない内容を無制限に受け入れるGENERAL_MEMORYは作らない。型・保存policy・privacy・管理方法を
定義してから採用し、Factの追加によってドメイン台帳や全会話本文を無条件に保存しない。

### 3. domain recordを人格記憶から分離する

`provider_id`はデータの所有境界を表し、記憶の意味分類には使用しない。

既存Wave 2では次の値から開始する。

```text
core
temporary:agriculture
temporary:recipe
```

- `approved_memories`は人格側が所有する記憶だけを持ち、`provider_id=core`とする
- 農業日誌とレシピは別の`temporary_provider_records`で保持する
- addon完成後は、`temporary:*`のrecordを対応するaddon DBへ移行し、暫定recordを物理削除する
- addonはpersona memory DBへ直接書き込まず、型付き`DomainEvent`等の共通境界を利用する
- persona memoryへ形成したepisodeは、元recordの移行後も`provider_id=core`のまま残す
- provenanceには元の`source_provider_id`とsource IDを保持する

すべてのdomain recordをepisodeへ変換しない。将来の関係的な応答に有用で、privacy検査を通過した経験だけを候補にする。
会話外活動ログからの実生成は#249、Episode / Factの共通登録は#340で行う。

例えば「トマトを12.4kg収穫した」という正確な数量は農業recordに保持し、persona memoryには
「ユーザーと今季最初のトマト収穫を迎え、一緒に成果を喜んだ」のような関係上の出来事を保持する。

判断理由は、domain recordには正確な訂正、集計、schema migrationが必要なのに対し、persona memoryは
関係上の経験を保持するためである。addon完成後に記録の所有先を移しても、人格側の経験まで失わないよう、
所有者とprovenanceを分ける。Factも外部domainの正本を置き換えない。

### 4. SQLite正本のschemaと新モデルの適用境界

既存Wave 2の`approved_memories`、`memory_sources`、`memory_lineage`、`memory_write_receipts`、
`memory_index_outbox`、`temporary_provider_records`は専用の`persona-memory.db`に配置する。
`conversation-history.db`とはファイルを分け、persona memory側のロールバックで会話履歴を巻き戻さない。
memory行とoutbox行を同一ファイル内で更新し、トランザクション原子性を保つ。

以下は既存Wave 2のフィールド契約であり、新モデルの物理schemaを固定するものではない。

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

新モデルのID・内容版・出典版・参照失効は2026-09 ADRを正本とする。
Episode / Fact / Episode–Fact参照 / Fact統合関係は#342、Semantic共通schema・formation_typeは#345、
Reflection専用正本は#292が所有する。旧formation_methodをDIRECT_EXTRACTION / EXPERIENCE_DERIVEDと混同しない。

Episodeのoccurred_at相当は今回の経験日時、Factの対象日時は話題の出来事の日時とする。
experienced_at / stated_at / created_at / last_user_mentioned_atも区別する。
タイムゾーンはconfigで定義し、元発言日時から相対日時を解釈する。使用timezone・部分日時・精度を保持し、
config変更や再試行で黙って解釈を変えない。日付・時刻・精度をすべて既知またはすべてNULLにする旧制約を新形式へ強制しない。
不明な対象日時を発言・登録日時で埋めず、月精度を月初の特定日や出来事の継続時間とみなさない。

#11のschema version 2や当時の適用方式は完了時点の履歴であり、今回のDB再作成許可ではない。
#289も合意時点ではEpicブランチのみの未リリース旧形式であり、旧形式の運用データ移行・後方互換を要求しない。
mainからの安全な適用と、会話履歴・temporary_provider_records・他データの保護は維持する。
想定外のデータを自動破棄・変換せず、DB全体を初期化しない。

provenanceは自由形式JSONだけに埋めず、型付き参照で会話turn、addon event、provider record等との関係を保持する。
各参照元の版を追跡し、複数sourceを単一parent_idやconsolidated boolだけで表現しない。

既存Wave 2のmemory_lineageはCONSOLIDATED_FROM / SUPERSEDES / DUPLICATE_OFを持つ。
新しいEpisode派生lineageやFact統合関係は各所有Epicで拡張する。Fact統合を既存memoryの置換と同一視しない。

Issue #8のMemoryWriteContextによる型付きsource・lineageと、save / correctで正本・source・lineage・
write receipt・outboxを同一transactionへ入れる原則を再利用する。
memory_write_receiptsは過去の保存・訂正の再試行で本文やoutboxを二重更新しないための不変記録である。

ChromaにはSQLiteのmemory_id、normalized_text、embeddingと必要最小限の検索metadataだけをmirrorする。
last_user_mentioned_atだけが変わる場合、Chromaは更新しない。
新モデルではSQLite正本に加え、Fact / source / 統合関係の有効性を取得時に検証する。
根拠の訂正・削除時は依存結果を即時利用停止し、Chroma更新の完了を待たない。

既存Wave 2ではLLM推定のimportance / confidence、心理状態のemotional_valence / emotional_intensity、
access_count / last_accessed_at、自由形式graph、学習memoryのpinned常時注入を追加しなかった。
新しい#341/#100の根拠評価metadataは別契約とし、LLMの自己申告値でprivacyや根拠条件を迂回しない。
Factの型付きID関係は、自由形式のentity graphを無制限に導入する許可ではない。

### 5. allowlistを保存同意として扱い、個別確認・通知を行わない

次の条件をすべて満たす候補を自動保存する。

```text
保存policyで許可された型
+ privacy検査通過
+ 対象sourceに保存拒否指示なし
+ conversation由来の場合はsource turnの履歴本文が保存済み
= 保存可能な検証済みCandidate
```

候補ごとのユーザー確認、保存通知、確認待ち候補の永続化、確認TTLは実装しない。
PENDING_CONFIRMATIONも保存判定から削除する。

ユーザーの「覚えて」は候補抽出を促すが、絶対禁止、意味分類、allowlistを迂回しない。
保存内容は記憶一覧UIから閲覧、訂正、物理削除できるようにする。
新しいUIは#343 / #348 / #351で各Epicごとに確認し、#296の横断Issueを復活させない。

判断理由は、保存対象を限定し、privacy判定をfail-closedにし、保存後の管理手段を用意すれば、
候補ごとの確認で会話を中断する負担を避けられるためである。

### 6. 保存拒否指示はcurrent turnの履歴と記憶形成へ適用する

用語上の指示は次のscopeとする。

| 指示 | scope | 処置 |
|---|---|---|
| 「覚えないで」「記憶しないで」 | RAG | persona memory／RAGへ保存しない。履歴は通常policyに従う |
| 「履歴に残さないで」 | BOTH | 履歴本文、persona memory／RAG、current turn由来の暫定domain recordへ保存しない |
| 「保存しないで」「記録しないで」 | BOTH | 履歴本文、persona memory／RAG、current turn由来の暫定domain recordへ保存しない |

MVPのStorageScopeはRAGとBOTHに限定し、HISTORYは削除する。効力はcurrent userのcurrent turnだけとする。
保存拒否があるturnでは、既存記憶へのlast_user_mentioned_at更新も行わない。

conversation由来の形成には、元turnの履歴本文が保存済みであることを必須条件とする。

```text
conversation-derived ALLOW_STRUCTURED
  => source turnがcompleted
  AND source turnの履歴本文が保存済み
```

履歴用sanitizerがSKIP_CONTENTを返した場合、turnがprivacy_skippedになった場合、または履歴保存を拒否した場合は、
候補抽出、TOUCH、暫定domain record作成を含む後続の永続化を行わない。
assistant側の検査結果でturn全体の履歴本文を消去した場合も同様とする。
スレッド抽出へ変更しても、対象turnごとの拒否・保存状態を保持し、周囲の許可済み発言で制約を洗い流さない。

判断理由は、元の履歴が存在しないのに長期記憶だけが残る状態は保存意図と一致しないためである。
HISTORYだけを拒否してRAGを許可するscopeは設けない。「覚えないで」は履歴を残しつつ長期化を止める要求として有効なためRAGを残す。
addon自身のrecordから発生するDomainEventはconversation由来ではなく、addon側policyとCore admissionを適用する。

### 7. classifierとadmission evaluatorの責務を分離する

Wave 1の決定論的privacy scanner、SemanticPrivacyClassifier、RagAdmissionEvaluatorは別の責務を持つ。

| component | 責務 | 行わないこと |
|---|---|---|
| Wave 1 privacy scanner | 秘密値、直接識別値、保存拒否指示を決定論的に検出 | 文脈依存の意味分類、保存先の最終決定 |
| SemanticPrivacyClassifier | health、心理状態、第三者情報、暗示表現等を文脈から分類 | allowlist変更、保存可否・権限の決定 |
| RagAdmissionEvaluator | scanner finding、semantic assessment、候補型から最終結果を決定論的に算出 | LLM呼び出し、DB／embedding／Chroma操作 |

SemanticSignalScreenerやkeywordによる意味的な前段filterを設けず、allowlist候補をすべてSemanticPrivacyClassifierへ渡す。
暗示表現がclassifierへ届かない経路を作らない。最適化が必要な場合はencoderによる前段実装を検討するが、
Wave 1 scannerは絶対禁止と明示的保存拒否の境界として残す。

Wave 1 scannerが絶対禁止または保存拒否を確定した場合はclassifierを呼ばず直ちに拒否する。
classifierのtimeout、モデル未ロード、未知言語、不正出力、未知カテゴリはABSTAIN_UNKNOWNとし、保存しない。

| 状態 | 意味 | 永続化副作用 |
|---|---|---|
| DENY_SENSITIVE | 機微情報または絶対禁止 | なし |
| DENY_USER_REQUEST | current turnの保存拒否 | なし |
| ABSTAIN_UNKNOWN | 判定不能またはclassifier障害 | なし |
| NOT_MEMORY_WORTHY | 安全だがallowlist外または長期的価値なし | なし |
| ALLOW_STRUCTURED | allowlistへ正規化されprivacy検査通過 | 検証済みCandidateを生成可能 |

ALLOW_STRUCTUREDだけがrepositoryへ到達できる。LLMはcharacter_id、provider_id、保存先、allowlistを選択しない。
意味理解のmodelと、保存権限を検証するapplicationを分ける。Factの同一性候補も検証なしに正本化しない。
統合見送りはprivacy拒否とは別であり、保存条件を満たす欠損Factまで捨てない。

### 8. 意味分類器はlocal LLMで開始し、versionを分離して記録する

既存Wave 2のSemanticPrivacyClassifierはローカルOllamaのgemma4:e4bを会話モデルと共有して開始する。
分類器が会話と競合して応答遅延やtimeoutを発生させる場合は会話を優先し、必要なら専用instanceへ分離する。

判定のprovenanceは結合文字列にせず、次の独立fieldで表現する。

```text
classifier_version
model_id
model_digest
prompt_version
policy_version
```

Wave 1 scannerの結果だけで終了しclassifierを呼ばなかった場合、semantic provenanceはNULLとする。
既存Wave 2契約のpolicy versionは2026-08-wave2-v1であり、HISTORY scopeの削除と同じ版で管理する。
新モデルのpolicy / schemaの版は実装時に定め、文書更新だけでruntimeの版を変更しない。

機微な本文を外部APIへ送らず、既存modelを流用して構成を小さくする。versionを独立fieldにするのは、
model・prompt・classifier・policyのどの変更が判定差を生んだかを切り分けるためである。

Issue #105以降もprivacy Targetはlocal Provider専用である。Ollama Client直接呼出しと用途別Model envは
共通Inference Target経路へ置き換えるが、この安全境界は維持する。

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

現在のuser原文は応答生成中だけ使用できるが、原文、原文hash、マスク前本文をSQLite、log、例外へ残さない。
userとassistantの双方に同じ履歴用scanner／sanitizerを適用する。

#340の新しい非同期形成契約は次とする。

```text
共通Conversation History保存・スレッド更新
  -> #291: 非同期予約、許可済みthread snapshot取得、必要な長文分割
  -> Episode / Fact候補抽出と元発言ID・版・範囲の対応
  -> candidate schema・Wave 1 scanner・semantic privacy・sourceの検証
  -> #290: 同一character・同一threadのFact照合と登録判定
  -> #342: Episode / Fact / 参照 / 有効な統合関係 / 必要なoutboxを保存
  -> index worker
```

候補抽出器とclassifierは別component。現在+直前turnだけへ限定せず、スレッド全体を対象にする。
長文の古い部分を黙って捨てず、分割境界の二重登録を防ぐ。元発言の拒否・版・有効性は維持する。
RAG検索結果は新規経験の根拠にしない。保存文は構造化値から所有者・行為者・対象・時刻が混同されない形で生成する。
未検証候補や生source本文を通常の記憶正本・ログへ転記しない。拒否候補の本文は保存しない。

同一スレッドの未処理予約を集約し、処理中更新は再予約する。失敗・再起動で未処理版を回復する。
thread revision増加だけで処理済み元発言から記憶を増殖させず、新発言と訂正を取りこぼさない。
sourceの版・有効性を保存直前にも確認し、古い候補・Fact同一性判定を適用しない。

相対日時はconfigのtimezoneと元発言日時を基準に解釈する。worker実行日時を基準にせず、部分日時・精度を保持する。
元発言やFactの対象日時と、話を聞いた経験の日時を区別する。

既存非同期classifierのbounded retry初期値は1回15秒、最大2回、全体35秒以内、queue滞留5分以内で、設定変更可能である。
新workerの回復・再予約と区別し、上限超過や判定不能で保存条件を緩めない。
sourceが削除・本文非保存・privacy_skippedへ変化していたら古い形成結果を保存しない。

Direct Semantic Extractionは#341、保存済みEpisodeからの一般化・内省の夜間形成は#100、別thread Fact整理は#354である。
各pipelineの生成・保存責務を#291へ混在させない。

### 10. 機微なcurrent queryではRAG検索を行わない

現在のuser発言が機微である場合、保存済み記憶の検索自体をskipする。ローカルembeddingだけを許可して
検索結果を再検証する方式は採用しない。

- Wave 1の絶対禁止findingまたはScanFailure: RAG検索をskip
- semantic assessmentがSENSITIVEまたはABSTAIN: RAG検索をskip
- semantic assessmentがNOT_SENSITIVE: RAG検索を許可

検索前classifierは同期経路のため短いtimeoutで1回だけ呼び、retryしない。初期timeoutは3秒とし、
timeout、不正出力、モデル未ロード時はRAG検索だけをskipして通常会話を続ける。
同じsourceに対する非同期admissionで安全に再利用できるassessmentは再利用してよい。

機微queryをembeddingしてindexへ問い合わせた時点で、機微な意図と既存記憶の関連付けが発生するため、
検索後の再検証だけでは境界を守れない。判定不能を含め検索前にskipし、会話そのものは継続する。

### 11. 検索順位は意味的関連度を主、最終言及日時をtie-breakとする

既存検索順位の原則を維持する。

1. Chromaの意味的関連度で候補を取得する。
2. memory_idでSQLite正本を引き直し、character_id、状態、TTL、policy version、本文の決定論的再検査を通す。
3. 新モデルではFact・source・統合関係・派生根拠の有効性と版も確認し、古いindexから無効情報を返さない。
4. 関連度閾値を満たす候補を意味的関連度の降順で並べる。
5. 同等関連度の候補間だけlast_user_mentioned_atの新しさを優先する。
6. さらに同順位ならcreated_at DESC、id ASCで安定順序を作る。
7. 最大件数とprompt token budgetの範囲で注入する。Reflectionは通常会話へ直接注入しない。

```text
semantic_relevance DESC
-> last_user_mentioned_at DESC NULLS LAST  # 同等関連度内だけ
-> created_at DESC
-> id ASC
```

同等関連度の許容幅は#9の固定検索corpusで決め、設定値として持つ。新しさを理由に明確に関連度の低い記憶を繰り上げない。

#11の既存時間検索は、決定論的パーサで時間条件を抽出できたqueryについてSQLite期間検索とChroma意味検索をunionする。
一致種別は両方一致、意味一致、期間一致の順。同種別では意味的関連度を主にし、意味距離のない期間一致同士では
last_user_mentioned_atをtie-breakにする。時間条件なし・パース失敗時はChroma一次、SQLite再検証へ縮退する。
query解析の同期経路へLLM往復は追加しない。

日時検索の根拠はSQLite正本とし、Chromaの日時metadataは整合確認の補助に限る。
新モデルでは経験日時とFact対象日時を区別し、部分日時を確定日時のように表示・照合しない。
promptには検証済み日時と精度を併記し、不明な日時を補完しない。両経路が正常終了して0件なら該当なしと推測禁止を明示する。
検索で使用したmemory ID・日時・精度・一致種別は既存metadata-only policyに従って追跡し、query・memory本文をlogへ残さない。

既存季節照合はoccurred_atの月から導出し、春3〜5月、夏6〜8月、秋9〜11月、冬12〜2月とする。
冬は年を跨ぎ、「去年の冬」は前年12月から当年2月までとする。YEAR精度またはNULLは季節照合から理由付きで除外する。
既存TemporalPrecisionのYEARからSECONDまでの6値と、新しい部分日時の物理表現は区別し、#342で整合させる。

last_user_mentioned_atはユーザーの明示的な新規言及・再言及・訂正でのみ更新する。
検索、prompt注入、assistant言及、addon更新、consolidationでは新しい言及日時を作らない。
新しいuser由来記憶は言及日時を設定し、addonだけ由来の場合はNULLから開始する。
consolidationではsource群の最大日時を引き継ぐ。

検索回数による強化、自動減衰・忘却、直近性・重要度・関連度の固定重み0.5 / 2 / 3は採用しない。
現在の問いとの関連性を主とし、検索された記憶がさらに強まるfeedback loopを作らない。

### 12. 同thread登録時照合と、保存後の非同期整理を分ける

再試行による二重保存防止は、意味的な出来事同定とは別の制御である。
元発言ID・版・抽出範囲と保存結果を対応づけ、同じ元入力の再処理でEpisode / Fact / link / 統合関係を増やさない。
thread revisionやcandidate_indexだけを新経験の証拠にしない。生本文hashをidempotency keyへ使わない。

既存Wave 2の型ごとのnatural keyとTOUCHは既存型の契約であり、新しいFactの同一性判定を代替しない。
TOUCHはlast_user_mentioned_atだけを更新し、本文・構造化値・content_version・updated_atを変更せず、Chroma outboxも作らない。
保存拒否・privacy拒否のturnでTOUCHしない条件は維持する。

新しいMVPのFact照合は#290が#291の非同期抽出・登録ジョブ内で行う。
同一character・同一threadに限定し、Factの5Wと文脈から同一出来事への再言及と確認できたときだけIDでまとめる。
unknown同士、日/月の範囲包含、同名だけ、同日別回の可能性は統合根拠にしない。
統合できなくても保存可能な情報は保持し、曖昧な追加の破棄・自動日時補完はしない。
会話応答は完了を待たず、同thread照合を別の夜間整理待ちにもしない。

同一character・別threadの保存済みFact統合は#354の後続非同期jobへ分離する。
#48の基盤を再利用可能だが、Fact固有の照合・ID解決・失効処理を実装済みとは扱わない。
Fact統合では元Episode・元Fact ID・出典を維持し、Episode自体の統合・削除は行わない。
#354は#340 / #341 / #100のMVP完了条件ではない。

既存#48のpersona memory整理はprovider_id=coreだけを対象にし、temporary / addon recordを除外する。
型付きplanは次を維持する。

```text
KEEP / MERGE / SUPERSEDE / DELETE_EXACT_DUPLICATE / CONFLICT / NOOP
```

applicationがtype、character、version、privacy、lineageを検証して適用し、曖昧な候補を自動削除しない。
詳細な既存consolidation契約は第16節を参照する。Fact統合をこのmemory置換処理へ無条件に読み替えない。

### 13. promptfooとpytestの責務を分けてclassifierを評価する

意味分類器のconformance testはpromptfooを用い、通常unit testへ実model評価を混在させない。

```text
backend/evals/privacy_classifier/
├─ cases.jsonl
├─ prompt-lab.yaml
├─ conformance.yaml
├─ provider.py
└─ README.md
```

- prompt-lab.yaml: productionの失敗構造を合成case化し、redから最小prompt修正でgreenへするtuning用
- conformance.yaml: production classifier、parser、version伝搬を含めて評価するrelease gate
- pytest unit: prompt組立、schema、parser、fail-closed、RagAdmissionEvaluatorをfakeで検証
- integration test: 実サービスに接続するsmoke caseを通常unitと分離

実ユーザー本文をcorpusへコピーしない。機微caseと安全caseを対で用意し、enumは決定論的assertionで検査する。
規則で表現できる判定へLLM-as-judgeを使用しない。

- 固定した機微caseがNOT_SENSITIVEになることを許容しない
- SENSITIVEまたはABSTAINは安全側の結果として許容する
- timeout、未知言語、未ロード、不正出力が保存許可にならない
- 安全候補の過検知は記録して改善するが、機微caseを保存可能にしてrecallを上げない

重要な変更では対象suiteを3回反復してから全suiteを実行する。
release時はmodel / prompt / policyの版を固定し、cache無効の全case反復結果を残す。
promptfooは固定versionのdevelopment dependencyとし、結果や本文を外部共有しない。

新しい記憶形成の最終受入は#344 / #349 / #297で行い、UIも#343 / #348 / #351で個別に検証する。
mock / fixture / readiness / DB直接投入だけの結果を、実LLM抽出から保存までの成功と同一視しない。

### 14. 会話品質への影響を観測してから最適化する

非同期admission workerのconcurrencyは1から開始し、会話処理を優先する。少なくとも次をmetadata-onlyで観測する。

- 会話応答のp95 latency
- classifierのqueue待ち時間とtimeout率
- model待ち時間
- GPU／memory使用量
- admission decision、reason code、model／prompt／policy version

会話遅延、classifier timeout、queue滞留が続く場合、専用instanceまたはencoderによる前段最適化を検討する。
固定検索重み、access強化、減衰、semantic screenerは、問題が観測される前には追加しない。
先に構成と評価軸を増やさず、実測から必要な最適化を選ぶ。

### 15. 実施順序と責務の移行

既存Wave 2の実施順は#28の履歴として保持する。

```text
#25 -> #50 -> #22 -> #33 -> #8 -> (#29 || #30) -> #31 -> #9 -> #10 -> (#11 || #12) -> #28受入
```

#50はdev/testとdogfoodのdata root・port・process ownership・backup/migration境界を分離した。
以降、privacyとadmission、SQLite正本、Chroma同期、検索、自動形成、時系列照合、UIを整えた。
#48はその後の既存persona memory整理であり、完了履歴を維持する。

新モデルは#352のADRを共通前提に、#340（#342 -> #290 -> #291、UI #343、受入 #344）と
#341（#345、#346、#347、UI #348、受入 #349）を分離する。
#100は保存済みEpisodeと共通Semantic Storeを利用し、#292 / #293 / #294 / #295、UI #351、受入 #297を担当する。
#354を待つ依存列を追加しない。進捗はIssue、実装済みruntimeはsystem-architecture.mdで扱う。

### 16. core persona memoryをidle時に原子的にconsolidationする

本節は完了済み#48の既存記憶レコード整理契約であり、新Fact統合の実装ではない。

provider_id=coreのactive persona memoryだけをcharacterとmemory typeごとに分離し、夜間または一定idle時間後にconcurrency 1で整理する。
会話処理、非同期admission、Chroma outboxに未処理作業がある間は起動せず、実行中に優先作業が生じた場合は次のmemory単位へ進む前に停止する。

planはKEEP、MERGE、SUPERSEDE、DELETE_EXACT_DUPLICATE、CONFLICT、NOOPの6種。
Provider / Model選択は2026-09のInference ADRを適用し、applicationがcharacter、provider、memory kind/type、
content version、privacy、source、lineageをSQLite正本から再検証する。
CONFLICT / NOOPはDBを変更せず、物理削除は本文・構造化値・時間情報が完全一致する場合だけ許可する。

MERGE / SUPERSEDEはformation_method=CONSOLIDATEDの新規行を作り、元行をINACTIVEにする。
新行から元行へCONSOLIDATED_FROM / SUPERSEDESを張り、source_type=CONSOLIDATION、source_provider_id=core、
元memory idのsource_refを保存する。新規行、source、lineage、write receipt、旧行状態変更、新旧outboxは単一transactionで確定する。
旧行にもUPSERT outboxを作り、index workerがinactive行をChromaから削除する。

既存idempotency keyは、consolidation、character id、plan種別、prompt version（consolidation-v1）、digestをコロンで連結する。
digestはmemory idとcontent versionの組を昇順に並べ、各組を< memory_id >:< content_version >の形式（山括弧と空白は含めない）で
改行連結した文字列のSHA-256である。character idとprompt versionにコロンを許可せず、memory idをkeyへ平文で含めない。
同じ入力snapshotの再実行で新規行やDELETE outboxを重複生成しない。

#48のschema更新はSCHEMA_VERSION=2を維持し、sqlite_masterのmemory_sourcesのCREATE TABLE文にCONSOLIDATIONが
含まれるかで更新要否を判別する方式だった。未更新の場合のみ同一transactionで旧tableをrename、新CHECK制約のtableへ
全行移送して旧tableを削除する。途中失敗はrollbackし再実行する。
これは当時の更新契約であり、#342 / #345の新schema適用方式を固定しない。

ログは件数、plan種別、latency、reason code、model / prompt / policy versionに限定し、CONFLICT時だけ対象memory idを記録する。
memory本文、prompt、model出力全文を記録しない。Fact固有の監査も既存privacyを弱めない。

## 新モデルのMVPで実装しない項目

- 候補ごとの同意確認、保存通知、確認待ち状態
- SemanticSignalScreenerや決定論的な意味keyword gate
- access countによる強化、自動減衰・忘却、固定の複合検索重み
- raw会話turnをそのままepisodic memoryとして保存すること
- persona memory、domain record、task、procedural knowledgeを単一の汎用正本へ混在させること
- 別threadのFact統合（#354の後続非同期処理）と別character統合
- 曖昧な同一性による自動統合・日時補完・追加情報の破棄
- Fact統合によるEpisodeの自動統合・削除、Semanticへの自動昇格
- 通常会話へのReflection直接注入、例外的な自己説明検索
- 自由形式entity / association graph
- addon完成前にtemporary recordをpersona memoryへ移すこと

## 影響・トレードオフ

- 候補確認なしで記憶を形成するが、保存policyとprivacy gateを維持し、各Epicの管理UIで監査する。
- 欠損を許容して保存できる一方、unknownを一致扱いしないため自動統合は保守的になる。
- 独立性不明のFactを別の出来事と数えないため、生成できる一般化知識が少なくなる場合がある。
- 非同期抽出・登録と別thread整理を分け、会話の応答を待たせない。
- domain record、Episode、Fact、Semanticに内容が重なる場合も、正確な台帳・経験・取得情報・知識の責務を混同しない。
- SQLiteの版・出典・参照失効、Chromaとの同期が必要になる。
- 最終言及日時をtie-breakだけに使い、検索feedback loopを作らない。
- 明示削除・利用停止・通常の忘却を区別し、削除対象を統合履歴等から復活させない。

## 関連

- [Episode・Fact・Semantic境界](episode-fact-semantic-boundaries-2026-09.md) — #352の採用契約と優先範囲
- [Character Life共通契約](character-life-memory-personality-autonomy-2026-09.md)
- [RAG privacy方針](rag-memory-privacy-policy-2026-07.md) — 維持するprivacy・SQLite・Chroma・outbox境界
- [システムアーキテクチャ](../system-architecture.md) — 実装済みruntime
- [エンハンス計画](../enhancement-plan.md) — 分解・依存・実施順
- [テスト方針](../testing-policy.md) — unit / module / integration / E2Eの区分
- #340 / #341 / #100 — Episode・Fact / Semantic / 一般化・Reflection
- #354 — 同一characterの別thread Fact非同期整理
- #48 — 既存persona memory consolidationの完了履歴
- #289 — 合意時点でEpicのみの未リリース旧Episode形式
- #28および#22 / #33 / #8 / #29 / #30 / #31 / #9 / #10 / #11 / #12 — 既存Wave 2実装の履歴
