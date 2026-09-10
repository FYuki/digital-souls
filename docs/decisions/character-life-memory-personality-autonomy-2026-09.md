# キャラクター生活・経験・内省・人格・自律活動の共通契約 (2026-09)

## 状態

**ACTIVE**。

本ADRは、ユーザーと共に過ごすパートナー型AIとして、キャラクターが会話外でも経験を持ち、経験を整理・内省し、中期的な関心・意思と長期的な人格へ反映しながら活動するための共通契約を定める。

本ADRは実行ランタイムの製品選定を固定しない。LangGraph、DBOS、Letta等を比較する場合も、本ADRのデータ・安全・更新契約を共通の受入条件とする。

競合する範囲では次の既存ADRを部分的に改定する。

- `wave2-memory-formation-retrieval-2026-08.md`
  - 旧`EPISODIC_EVENT.subject=USER/SHARED/SELF`を廃止し、所有する`character_id`の視点と複数参加者・役割で経験を表す。
  - `reflection`を単なるformation methodとする記述を改定し、ReflectionをSemantic Memoryとは別の永続概念として扱う。
  - working memoryとは別に、中期的なLife Stateを定義する。
- `rag-memory-privacy-policy-2026-07.md`
  - 長期記憶保存時のprivacy policyは維持する。
  - 外部Tool/MCPへargumentsを送る直前に、別のEgress Privacy Checkを追加する。
- `tool-use-foundation-2026-09.md`
  - 会話外の自律活動でも#104/#182のCapability Snapshot、Tool Catalog、Execution Gateを再利用する。
  - 自律runtimeからMCPを直接呼び出さない。

## 1. 基本原則

主目的は業務代行ではなく、キャラクターの経験蓄積・整理・内省・人格形成と、ユーザー不在時にも継続する生活感である。

```text
ユーザーとの会話・共同作業 / キャラクター自身の活動
                         ↓
                    Episode
                         ↓
        ┌────────────────┴────────────────┐
        ↓                                 ↓
Semantic Abstraction                 Reflection
客観的な共通要素の蒸留          本人が経験をどう捉えたか
        ↓                                 ↓
Semantic Memory                  ┌────────┴────────┐
                                 ↓                 ↓
                           Life State          Personality
                        中期的な関心・意思      長期的傾向
                                 ↓                 ↓
                              Activity / Conversation
                                      ↓
                                  New Episode
```

Memory、Reflection、Personality、Relationship、Skill、runtime stateは同じ正本として扱わない。

## 2. Episode / Episodic Memory

### 2.1 所有するキャラクターの視点に固定する

Episodeは、所有キャラクター自身の視点で整理した経験の記憶である。会話、共同活動、
外部サイトでの交流という場所によらず、既存`approved_memories`の`EPISODIC_EVENT`へ保存する。
別のself episode tableは作らない。2026-09-10の#100決定で、従来の
`subject=USER / SHARED / SELF`分類を廃止した。

- `character_id`は所有者・経験の視点の絶対境界。保存文の主語もそのキャラクターに固定する。
- 同じSQLite内で各レコードを論理分離する。物理DB/schemaのキャラクター別分割は要求しない。
- 所有者と別に、複数の交流相手・参加者の識別情報、名称、役割を構造化して保持する。
- 既知のキャラクターはcharacter ID、外部の人物は特定できる場合に出所の範囲を含むIDを使う。
  ID不明時は名称を保持できるが、同名だけで同一人物と断定しない。
- 今回の経験の参加者と、話題の出来事に登場する人物を区別する。
- 専用の直接経験/伝聞二択カラムは追加せず、行為と参加者の役割で表現する。
- 保存文は新スキーマの所有者・参加者・行為を参照して生成する。旧subject接頭辞で主語を推測しない。

| 保存文の例 | 経験の意味 |
|---|---|
| 光織はユーザー、蒼と静岡へ行った | 光織の旅行経験 |
| 光織はユーザーと、光織・ユーザー・蒼で静岡へ行った思い出を語った | 光織が思い出を語った経験。旅行とは別Episode |
| 光織はユーザーから、ユーザーと蒼が静岡へ行った話を聞いた | 光織が話を聞いた経験。光織を旅行参加者にしない |
| 蒼も同じ話を聞いた | 蒼が所有する別Episodeとして蒼の視点で整理 |

語った経験は新しい交流の根拠にできるが、同じ旅行を二回の旅行として強化しない。

会話外経験向けevent typeに`OBSERVATION / ACTIVITY / ENCOUNTER`を追加し、
既存の`SHARED_MILESTONE / ACHIEVEMENT / DECISION / OUTCOME / CHANGE`は維持する。
旧event type名の維持は旧subject分類の維持を意味しない。

### 2.2 経験の時刻と話題の時刻を分ける

| 項目 | 定義 |
|---|---|
| `occurred_at / occurred_timezone / occurred_precision` | 今回登録する経験そのものの発生日時と精度 |
| `experienced_at` | 所有キャラクターがその経験を得た日時。時間減衰・継続性評価の基準 |
| `stated_at` | 元発言の日時 |
| `created_at` | SQLiteへの登録日時 |
| 関連情報（任意） | 同じcharacterの既存Episode ID、話題の出来事の発生日時・精度 |

旅行Episodeの`occurred_at`は旅行日時、思い出を語った/話を聞いたEpisodeではその会話の日時である。
過去の旅行を今日聞いた経験は、`occurred_at`も`experienced_at`も今日とし、過去の旅行日時は
任意の関連情報へ分離する。「聞いた過去の出来事の日時をoccurred_atにする」という旧例は廃止する。

発生日時不明を`stated_at`や`created_at`で補わない。日付だけが判明する場合は日精度を保持し、
同日という理由だけで別経験を統合しない。関連Episodeは特定できる場合だけ参照し、
参照先に日時がある場合はそれを正本とする。Episode未登録でも、話題の日時が判明すれば保持できる。

### 2.3 Source / provenance

既存`memory_sources`を再利用し、必要に応じて`AGENT_ACTIVITY / EXTERNAL_RESOURCE`を追加する。
活動ログとEpisodeは分け、全Tool callをEpisodeへ変換しない。

```text
Activity / Tool Execution
        ↓
活動ログの要約・保存条件・意味評価（#249）
        ↓
共通契約によるEpisode保存（#100）
```

外部情報を読んだ・見た・聞いたという本人の経験と、外部情報そのものの正本を混同しない。
元resource / activityへのprovenanceを保持する。#100は会話外経験を受け取れる共通契約を整備し、
会話外ログからのEpisode生成処理は#249で実装する。

### 2.4 登録時の重複判定と日時補完

所有者、対象人物・役割、今回の行為/出来事、発生日時・精度を照合する。同じtopicや同じ日だけでは
同一性を確定しない。一つの会話対象について整理し、対象の異なる経験を混ぜない。

| ケース | 登録時の扱い |
|---|---|
| 日時不明の同一人物・同じ出来事への反復言及 | 別Episodeとして登録できる。派生形成の根拠は1件 |
| 日時既知の同じ出来事へ日時不明の追加言及 | 曖昧な反復を追加登録しない |
| 連続する会話で別日・別出来事と判明 | 改めて別Episodeとして登録 |
| 日時不明Episodeへの日付補完 | 同じ出来事と特定できた場合のみ更新 |
| 旅行と思い出を語る会話 | 別の経験として登録。旅行の根拠を二重加算しない |

日時不明の実際は6月の経験を、似た7月の経験で上書きしない。同じ抽出結果の再試行は冪等とし、
別の言及の登録と再試行による二重保存を区別する。全provenanceを残し、後段で独立根拠数を評価する。

### 2.5 会話由来Episodeの抽出予約

抽出範囲は現在+直前turnではなくスレッド単位とする。スレッド更新で抽出を予約し、
会話応答は抽出・保存を待たない。Semantic / Reflectionの夜間実行とは別のトリガーである。

- 同一スレッドの未処理予約は一つに集約し、処理中更新は再予約する。
- 抽出元の版と保存時の版・有効性を確認し、訂正/削除と矛盾する古い結果を保存しない。
- 長いスレッドは分割抽出・統合し、古い部分の黙示的切り捨てや分割境界での重複を防ぐ。
- 未処理の版/更新を再起動・失敗後にも識別し、再予約できるようにする。完了済み処理は冪等にする。
- sourceのprivacy・保存拒否・character境界を維持し、RAG結果を新しい経験の根拠にしない。

## 3. Semantic Memory / Reflection / Candidate

### 3.1 Semantic Memory

Semantic Memoryは、複数のEpisodeから感情・意思・自己評価をなるべく排して抽出した、根拠付きの共通要素とする。

「客観的」とは外部世界の絶対的真実を意味せず、与えられたEpisode集合から根拠を追跡できる一般化を意味する。

Derived Semantic Memoryはprivacy・schema・evidence・policy検証完了後、既存`memory_kind=SEMANTIC`配下へ保存する。元Episodeは削除・置換しない。

lineageには`ABSTRACTED_FROM`等、派生元Episodeを追跡できるrelationを追加する。

### 3.2 Reflection

Reflectionは、本人が複数のEpisodeをどう認知・評価し、何を感じ、何を大切にし、今後どうしたいと捉えたかを表す。

ReflectionはSemantic Memoryとは別概念であり、`approved_memories`とは別の永続model/tableへ保存する。同じ`persona-memory.db`へ保存してよい。

ReflectionのMVP lifecycleは次とする。

- `ACTIVE`
- `SUPERSEDED`
- `INACTIVE`

再内省により認知が変わった場合、旧Reflectionを物理削除せず`SUPERSEDED`とし、新しいReflectionを`ACTIVE`にする。置換関係と根拠の版を追跡する。
同じ論点の再解釈だけを置換し、異なる観点のReflectionは共存できる。

再内省は新しい関連Episodeと根拠訂正/削除を契機にする。MVPではPersonality変更だけでは
実行せず、実行時に現在のPersonalityを参照できる。旧Reflectionは補助情報として使えるが、
独立したEpisodeの根拠数には加算しない。

Reflectionは通常会話のRAG/promptへ直接注入しない。Current InterestsやPersonalityを通じた
間接利用の境界を維持し、例外的な自己説明検索は#100で追加しない。

Reflectionは自由文とprovenanceを正本とし、Insight / Interest / Intentionを同じ自由文へ埋め込んで更新し続けない。

### 3.3 Reflectionからの派生

Reflectionから次の概念を分離して派生可能とする。

- `Insight`: 自己洞察。「自分はどういう傾向・価値を持つようだ」と理解した内容。
- `Current Interests`: 現在注意・好奇心が向いている対象。Life StateのInterestへ渡す結果。
- `Goal Intention`: 今後どうしたいかという目標意図。
- `Implementation Intention`: 「XならYする」という条件付きの実行意図。

Reflectionは過去の認知、Intentionは未来の方向・行動方針である。
#100がprovenance・有効性を伴う検証済み派生結果を作り、Life Stateとしての運用は#249、
Personalityへの反映は#101が担当する。

### 3.4 Candidate

Candidateは、LLM等が生成したが、privacy・evidence・schema・policy検証前のデータである。

Candidateを承認済みの長期記憶やReflectionとして検索・prompt・人格更新へ利用しない。
検証に通った候補は自動保存・有効化し、候補ごとの手動承認は設けない。
拒否候補の本文は保存せず、日時・拒否理由・元Episode IDだけを監査情報に残す。

Semantic/Reflectionは、派生元Episodeが保存可能であってもprivacyを再評価する。複数の安全なEpisodeから機微情報を推論する可能性があるためである。

## 4. Episode bundle / cluster

Semantic Abstraction / Reflection形成時の「cluster」は永続的なcluster entityではなく、今回一緒に評価するEpisode集合とする。

基本手順は次とする。

1. 新しいEpisode等をanchorにする。
2. Chromaでsemantic近傍を取得する。
3. SQLite正本で`character_id`、memory type / event type、対象world・人物/役割/行為、時刻、有効性、source revisionを検証・絞り込む。
4. 独立した2件以上のEpisodeが揃った場合にSemantic / Reflection候補を生成する。件数だけで採用を確定しない。

`character_id`は絶対境界とする。timeはclusterを分断する絶対条件ではなく、検索範囲・関連性・長期継続性評価に利用する。

近傍取得数、類似度threshold、time window等は調整可能な設定値とする。

日時不明の同一出来事、別スレッド/別発言での反復、同じ旅行を語る経験によって旅行の根拠数を
増やさない。同一出来事は1件として評価し、全provenanceを残す。気質/性格による重みもその経験へ
一度だけ適用する。Temperamentは固定、Personalityは可変という用語契約を維持し、経験への着目・
主観的な評価への利用を許容するが、Semanticの事実的根拠を水増ししない。

矛盾する経験がある場合は根拠のある条件付き共通点へ限定し、説明できなければ形成を見送る。
派生形成の根拠は保存済みEpisodeとし、会話履歴から一般化知識や人格deltaを直接生成しない。

### 4.1 夜間実行と監査表示

Semantic / Reflectionの形成と再評価はMVPでは夜間の固定時刻に実行する。時刻・timezoneを設定し、
未処理/再評価対象を識別する。再試行・再起動・保存競合で同じ入力集合と版を二重適用しない。
将来の詳細な実行管理は#249が担当する。

既存の長期記憶管理画面を種類別タブで拡張し、Episode / Semantic / Reflectionの内容、根拠、評価、
状態、置換履歴を監査できるようにする。既存の訂正/削除は維持し、新しい手動編集・候補承認は追加しない。

## 5. Life State

working memoryとは別に、会話終了後も中期的に継続するLife Stateを持つ。

MVPは次を対象とする。

- `Interest`
- `Ongoing Activity`
- `Goal Intention`
- `Implementation Intention`
- `Next Action Candidate`
- `Share Candidate`

Interestだけでは外部活動を開始しない。外部活動は原則としてGoal Intention以上を必要とし、Implementation Intentionで実行条件を具体化できる。

Intentionは少なくとも`ACTIVE / COMPLETED / ABANDONED / SUPERSEDED`のlife cycleを持つ。Interestの減衰・dormant化の具体値はdogfoodで調整する。

Behavioral Changeは保存された意思そのものではなく、後続Episode群から観測される実際の行動傾向の変化とする。

## 6. Mutable Personality

### 6.1 正本はBig Five Aspects 10因子

Mutable PersonalityはBig Five Aspectsの10 Aspectを正式な内部traitとする。

- Neuroticism: `Volatility`, `Withdrawal`
- Agreeableness: `Compassion`, `Politeness`
- Conscientiousness: `Industriousness`, `Orderliness`
- Extraversion: `Enthusiasm`, `Assertiveness`
- Openness/Intellect: `Openness`, `Intellect`

各traitの内部値は`[-1.0, +1.0]`とする。

Immutable Coreは別に保持し、経験で変更しない。Mutable Personalityの数値をそのままLLMへ提示する必要はなく、PromptBuilderが自然言語の人格コンテキストへ変換する。

### 6.2 Egogram

Egogram（CP/NP/A/FC/AC）は人格パラメータの正本にしない。

Big Five Aspectsから生成された人格コンテキストと、実際の会話・行動表出の一貫性を質的に評価する評価機として利用する。Egogram結果を直接Personality Deltaへ入力しない。

### 6.3 Personality Delta

LLMに数値deltaを自由決定させない。

Personality更新の主入力は現在`ACTIVE`なReflectionと、その根拠となる独立Episodeとする。

同じEpisodeが複数Reflectionから同じtraitへ参照される場合、`episode × trait`単位で最新の有効Reflectionのみを採用し、重複加算しない。別traitを支持する場合は別証拠として利用可能とする。

Episodeの現在の証拠重みは`experienced_at`基準の指数減衰を基本とする。

```text
w_i(t) = 2 ^ (-(t - experienced_at_i) / half_life)
```

支持・反証を同じ方式で集計し、thresholdを超えた場合だけboundedなPersonality Delta候補を作る。

次は設定値とする。

- `half_life`
- `min_support`
- `update_threshold`
- `max_delta_per_update`
- `cooldown`

契約として次を固定する。

- 単一Episodeだけで強い人格変更を確定しない。
- 同じEpisodeを同じtraitへ複数回加算しない。
- 相反するReflectionを無視しない。
- delta上限・更新頻度・許可traitをapplication側で検証する。
- change history / provenance / rollbackを保持する。

## 7. Relationship State / Interpersonal Skill

Relationship StateはPersonalityと分離し、相手ごとに保持する。

MVPは2軸とする。

- `affective_valence`: 感情の正負 `[-1.0, +1.0]`
- `relational_proximity`: 接近 / 分離 `[-1.0, +1.0]`

「距離を詰めるのがうまい」等はRelationshipの増減係数ではなくInterpersonal Skillとして扱う。

```text
Personality
= どう振る舞いがちか

Interpersonal Skill
= どうすれば対人行動を上手く実行できるか

Relationship State
= 特定相手との関係が現在どうなっているか
```

Interpersonal Skillの学習は#102のProcedural Memory / Skill pipelineへ含める。SkillはRelationshipを直接書き換えず、実際のInteraction → Episode / Reflectionを経てRelationship Deltaへ影響する。

## 8. Autonomy Target

会話外で外部へ活動できる対象を`Autonomy Target`として管理する。

対象には少なくとも次を含める。

- Web / external service
- MCP connection
- Addon

ユーザーがMCP connectionを自律利用対象として許可した場合、そのconnectionのactive Capability Snapshotに含まれるToolはread/writeを問わず利用可能とする。Tool単位のuser permission UIはMVPで作らない。

既存のMCP connectionが会話中に利用可能であることと、自律活動で利用可能であることは別設定とする。

Autonomy Targetはadd / disable / revoke可能とし、無効化しても過去Activity / Episodeのprovenanceを削除しない。

### 8.1 Capability変更

MCP Server側でToolが追加・削除・schema変更された場合もconnection単位のAutonomy Grantは維持する。

#104/#182のCapability Snapshot / revision / fixed-loop契約を再利用し、実行中loopのsnapshotへ新Toolを途中追加しない。次loopから新active snapshotを利用する。

Tool変更は#182系のバッチ表示・管理UI要件へ揃え、Toolごとの再承認UIを追加しない。

## 9. Minimum Disclosure / Egress Privacy

長期記憶保存時のprivacy admissionと、MCP / 外部Toolへargumentsを送信する直前のEgress Privacy Checkは別処理とする。

自発活動ではraw conversation history全体を外部へ送らず、承認済みEpisode / Semantic / Reflection / Life State等から目的に必要な意味だけをTool argumentsへ射影する。

```text
Memory / Reflection / Life State
        ↓
Intent
        ↓
Minimum Argument Projection
        ↓
Semantic Privacy / Egress Check
        ├─ ALLOW
        └─ BLOCK
        ↓
Execution Gate
        ↓
External Tool / MCP
```

MVPでは安全側へ倒し、機微情報は外部送信を`BLOCK`する。長期記憶Admissionで一度安全判定済みでも、複数情報から機微情報を意味推論してargumentsへ生成する可能性があるため、送信直前に意味レベルで再判定する。

credential、password、API key、token、private key等のsecretは常にBLOCKする。

ユーザーとの経験・承認済みMemoryであること自体を外部送信禁止理由にはしない。目的達成に不要な個人情報・機微情報を削ぎ、一般化した検索語等で目的を達成できる場合はMinimum Disclosureを優先する。

## 10. High Impact operation

Autonomy Grantは「このMCP / Targetを自律利用してよい」ことを表し、高影響操作の追加確認とは分離する。

高影響判定はLLM単独に委ねず、Coreが次の2段階で評価する。

1. capability登録時のstatic effect classification
2. dispatch直前のresolved binding / actual argumentsを含むruntime impact evaluation

MCP annotationはtrust評価済みeffective policyの補助情報として利用可能だが、未知・非信頼annotationのみで安全判定を緩和しない。

MVPでHigh Impactとする代表条件は次とする。

- 不可逆・復旧困難な削除、force、履歴破壊
- permission / ACL / security / credential変更
- 購入、予約、契約、課金等の金銭・法的commitment
- 大量・一括操作などblast radiusが大きい変更
- private→public等、重要な公開範囲変更
- Core自身の設定・credential・DB・Character Card等の保護境界変更
- 副作用があり、Coreが影響を分類できない操作

`external-send`または`public`であることだけを理由に常にHigh Impactとはしない。通常の投稿、DM、Issue更新、文書編集等は、許可済みTargetかつHigh Impact条件に該当しない場合は自律実行可能とする。

High Impactは#185のConfirmationPolicyへ委譲する。

## 11. Execution / Recovery / Result contract

外部Tool実行は既存#104 Execution Gateを必ず通す。自律runtimeからMCPを直接呼び出さない。

開始済みの副作用はstop / barge-inだけで取り消されたとは扱わない。外部結果不明を通常failureへ潰さず、#185 ActionRecoveryの`result_unknown`契約を利用する。

runtime間で共通化する結果状態は次を基本とする。

- `APPLIED`
- `NO_CHANGE`
- `DEFERRED`
- `SUPERSEDED`
- `CONFLICT`
- `REJECTED`
- `FAILED`
- `RESULT_UNKNOWN`

処理自体が正常完了しても、根拠不足等で`NO_CHANGE`となることを正常結果として扱う。

元Episodeの訂正・削除・非公開化では、依存するSemantic / Reflectionと派生結果をSQLite上で
即時に利用停止し、残存根拠からの再評価対象とする。Chromaの削除/再生成は後追いでよいが、
検索・コンテキスト注入・派生結果利用時にSQLite正本と参照先の有効性を確認する。
Life State / Personalityへの波及はそれぞれ#249/#101の境界で扱う。

関連Episodeが無効でも、その思い出を語った等の別経験を無条件には削除しない。参照を無効にし、
本文に削除情報が残る場合は利用停止・再評価する。無効な関連参照や古いruntime checkpointから
削除情報を正本・コンテキストへ復活させない。

## 12. Background priority / resource scheduling

優先順位は次とする。

```text
foreground conversation
  > user-requested activity
  > autonomous background activity
```

ただし、foreground開始時にbackground処理を強制pause / preemptするかはMVPの固定契約にしない。

GPU / VRAM / TTFT、音声stream、複数キャラクター会話、配信、別推論server等への影響を実測してからpreemption要否を決める。

将来はCPU_LIGHT / LLM_LIGHT / LLM_HEAVY / GPU_HEAVY / EXTERNAL_IO等のresource class導入を検討できるが、初期必須にしない。

## 13. Framework-neutral runtime boundary

LangGraph / DBOS / Letta等の比較では、次をフレームワークへ委譲可能とする。

- orchestration
- queue / scheduling
- wait / resume
- checkpoint / recovery
- candidate generation

一方、正本更新条件・privacy・lineage・personality delta上限・Autonomy Target・High Impact境界はdigital-soulsの共通契約として維持する。

フレームワーク採否は、本ADRを使った同一scenarioで比較して決定する。

## 14. 受入シナリオ

少なくとも次を検証する。

1. 複数EpisodeからDerived Semantic MemoryとReflectionを形成できる。
2. ReflectionからInterest / Intentionが形成され、外部活動につながる。
3. 本人の会話外活動が所有者の視点でsource付きEpisodeになる（共通契約は#100、生成処理は#249）。
4. 同じEpisodeについて再内省してもPersonality evidenceが水増しされない。
5. 反証がある場合はPersonality更新を弱化・保留できる。
6. Big Five Aspects 10因子の更新がboundedで監査可能である。
7. Egogram評価は表出一貫性の質的評価としてのみ使われる。
8. Relationship 2軸とInterpersonal Skillが分離される。
9. 許可済みMCPを自律利用でき、通常のupdateも実行できる。
10. Minimum Disclosure後にも機微情報を意味推論したargumentはBLOCKされる。
11. High Impactだけが#185確認へ流れ、通常のexternal-sendは自律活動を不必要に停止しない。
12. MCP Tool変更は次snapshotから反映され、Tool単位の再承認を要求しない。
13. Episode削除・訂正後に派生状態が再評価され、古いcheckpointから復活しない。
14. foreground優先を維持しつつ、background preemption要否を性能評価できる。

## 15. #100の実装境界と検証

| 責務 | 担当 |
|---|---|
| Episode共通契約・保存文・会話由来抽出の改修、Semantic / Reflectionと検証済み派生結果 | #100 |
| 会話外活動ログの要約からEpisode生成、Life State運用、実行管理拡張 | #249 |
| ReflectionからPersonality更新 | #101 |
| 同種記憶の整理・正規化 | #48 |

#48へGENERALIZEを追加せず、派生形成を別pipelineにする。addon/domain recordを無条件にpersonaへ
コピーしない。少数経験への一般化から、多数の経験を多角的に検討する発達段階の表現は将来課題とする。

#100では変換対象となる既存長期記憶データがないため、既存データ変換・旧形式互換復元・dogfood記憶
マイグレーションは不要とする。新規スキーマの定義/適用は行うが、会話履歴や他のデータの削除、
DB全体の初期化を許可する判断ではない。

受入は[テスト方針](../testing-policy.md)に従い、実LLMによる抽出/判定からSQLite保存までの経路と、
実Chroma検索・派生形成・無効化・管理画面を分離したテスト環境で検証する。モック成功、readiness、
DBへの候補直接投入だけで実接続完了としない。時刻/重複/根拠数/競合/回復は決定的な回帰テストも行う。
