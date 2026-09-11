# キャラクター生活・経験・内省・人格・自律活動の共通契約 (2026-09)

## 状態

**ACTIVE**。

本ADRは、ユーザーと共に過ごすパートナー型AIとして、キャラクターが会話外でも経験を持ち、経験を整理・内省し、中期的な関心・意思と長期的な人格へ反映しながら活動するための共通契約を定める。

本ADRは実行ランタイムの製品選定を固定しない。LangGraph、DBOS、Letta等を比較する場合も、本ADRのデータ・安全・更新契約を共通の受入条件とする。

競合する範囲では次の既存ADRを部分的に改定する。

- `wave2-memory-formation-retrieval-2026-08.md`
  - `EPISODIC_EVENT.subject=USER/SHARED`限定を改定し、本人視点の`SELF`経験を追加する。
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

### 2.1 本人経験を既存persona memoryへ統合する

本人が会話外で実際に行った活動・観測も、自伝的なエピソード記憶として既存`approved_memories`へ保存する。別のself episode tableは作らない。

`EPISODIC_EVENT.subject`は次を許可する。

- `USER`
- `SHARED`
- `SELF`

`SELF`はキャラクター本人が経験した出来事を表す。所有者は常に`character_id`で分離する。

MVPの本人活動向けevent typeとして次を追加する。

- `OBSERVATION`
- `ACTIVITY`
- `ENCOUNTER`

既存の`SHARED_MILESTONE / ACHIEVEMENT / DECISION / OUTCOME / CHANGE`は維持する。

### 2.2 `experienced_at`を追加する

既存の時刻は次の意味を維持する。

- `occurred_at`: 世界で出来事が起きた時刻
- `stated_at`: 根拠発言の時刻
- `created_at`: SQLiteへ登録・有効化した時刻

これに加え、人格形成・内省の時間基準として`experienced_at`を持つ。

`experienced_at`はキャラクター本人がその経験を得た時刻である。過去の出来事を今日初めて聞いた場合、`occurred_at`は過去、`experienced_at`は今日になる。

### 2.3 Source / provenance

既存`memory_sources`を再利用し、必要に応じて次のsource typeを追加する。

- `AGENT_ACTIVITY`
- `EXTERNAL_RESOURCE`

活動ログとEpisodeは分ける。全Tool callをEpisodeへ変換しない。

```text
Activity / Tool Execution
        ↓
保存条件・意味評価
        ↓
Episode
```

外部情報を取得した場合は、「本人がその情報を読んだ・見た・聞いた」という経験と、外部情報そのものの正本を混同しない。元resource / activityへのprovenanceを保持する。

## 3. Semantic Memory / Reflection / Candidate

### 3.1 Semantic Memory

Semantic Memoryは、複数のEpisodeから感情・意思・自己評価をなるべく排して抽出した、根拠付きの共通要素とする。

「客観的」とは外部世界の絶対的真実を意味せず、与えられたEpisode集合から根拠を追跡できる一般化を意味する。

Derived Semantic Memoryはprivacy・schema・evidence検証完了後、既存の長期記憶へ保存する。元Episodeは削除・置換しない。

lineageには`ABSTRACTED_FROM`等、派生元Episodeを追跡できるrelationを追加する。

### 3.2 Reflection

Reflectionは、本人が複数のEpisodeをどう認知・評価し、何を感じ、何を大切にし、今後どうしたいと捉えたかを表す。

ReflectionはSemantic Memoryとは別概念であり、`approved_memories`とは別の永続model/tableを第一候補とする。同じ`persona-memory.db`へ保存してよい。

ReflectionのMVP lifecycleは次とする。

- `ACTIVE`
- `SUPERSEDED`
- `INACTIVE`

再内省により認知が変わった場合、旧Reflectionを物理削除せず`SUPERSEDED`とし、新しいReflectionを`ACTIVE`にする。

Reflectionは自由文とprovenanceを正本とし、Insight / Interest / Intentionを同じ自由文へ埋め込んで更新し続けない。

### 3.3 Reflectionからの派生

Reflectionから次の概念を分離して派生可能とする。

- `Insight`: 自己洞察。「自分はどういう傾向・価値を持つようだ」と理解した内容。
- `Interest`: 現在注意・好奇心が向いている対象。
- `Goal Intention`: 今後どうしたいかという目標意図。
- `Implementation Intention`: 「XならYする」という条件付きの実行意図。

Reflectionは過去の認知、Intentionは未来の方向・行動方針である。

### 3.4 Candidate

Candidateは、LLM等が生成したが、privacy・evidence・schema・policy検証前のデータである。

Candidateを承認済みの長期記憶やReflectionとして検索・人格更新へ利用しない。永続化が必要な場合は検索対象外・短期保持の作業データとして扱う。

Semantic/Reflectionは、派生元Episodeが保存可能であってもprivacyを再評価する。複数の安全なEpisodeから機微情報を推論する可能性があるためである。

## 4. Episode bundle / cluster

Semantic Abstraction / Reflection形成時の「cluster」は永続的なcluster entityではなく、今回一緒に評価するEpisode集合とする。

基本手順は次とする。

1. 新しいEpisode等をanchorにする。
2. Chromaでsemantic近傍を取得する。
3. SQLite正本で`character_id`、memory type / event type、対象world・subject、時刻等を検証・絞り込む。
4. 独立した複数Episodeが揃った場合にSemantic / Reflection候補を生成する。

`character_id`は絶対境界とする。timeはclusterを分断する絶対条件ではなく、検索範囲・関連性・長期継続性評価に利用する。

近傍取得数、類似度threshold、time window等はdogfoodで調整可能な設定値とする。

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

High Impactは#185のConfirmationPolicyへ委譲する。2026-09-11の合意により、
承認を接続 × 通常/ハイリスク操作群 × 対話中/会話外で分離する。
通常操作群は初期「常に承認」、ハイリスク操作群は未承認とし、保存済み承認の効力も評価する。
単回承認、場面別拒否、確認キューと60秒待機、チャット内3択・後続管理UIの契約は
[`addon-action-approval-recovery-2026-09.md`](addon-action-approval-recovery-2026-09.md)を正本とする。

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

元Episodeの訂正・削除・非公開化が発生した場合、依存するSemantic / Reflection / Life State / Personalityを即座に物理削除するのではなく、stale / reevaluation対象とし、残存根拠から再評価する。古いruntime checkpointから削除済み情報を正本へ復活させない。

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
3. 本人の会話外活動が`SELF` Episodeとしてsource付きで保存される。
4. 同じEpisodeについて再内省してもPersonality evidenceが水増しされない。
5. 反証がある場合はPersonality更新を弱化・保留できる。
6. Big Five Aspects 10因子の更新がboundedで監査可能である。
7. Egogram評価は表出一貫性の質的評価としてのみ使われる。
8. Relationship 2軸とInterpersonal Skillが分離される。
9. 許可済みMCPを自律利用でき、通常のupdateも実行できる。
10. Minimum Disclosure後にも機微情報を意味推論したargumentはBLOCKされる。
11. 操作群・実行場面別の承認を評価し、初期状態ではHigh Impactが#185確認へ流れる。通常のexternal-sendを一律に停止せず、保存済み承認も適用する。
12. MCP Tool変更は次snapshotから反映され、Tool単位の再承認を要求しない。
13. Episode削除・訂正後に派生状態が再評価され、古いcheckpointから復活しない。
14. foreground優先を維持しつつ、background preemption要否を性能評価できる。
