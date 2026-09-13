# キャラクター生活・経験・内省・人格・自律活動の共通契約 (2026-09)

## 状態

**ACTIVE**。

本ADRは、ユーザーと共に過ごすパートナー型AIとして、キャラクターが会話外でも経験を持ち、経験を整理・内省し、中期的な関心・意思と長期的な人格へ反映しながら活動するための共通契約を定める。

2026-09-12の#352で記憶責務を再編した。Episode / Fact / Semanticの正本、5W、日時、ID参照、
Fact照合・統合、参照失効の詳細は[Episode・Fact・Semanticの正本と形成責務](episode-fact-semantic-boundaries-2026-09.md)を優先する。
本ADRはLife State・Personality・Relationship・自律活動との接続を所有する。
ACTIVEは採用設計を意味し、新しい記憶モデルがmain / dogfoodで実装済みであることを意味しない。

本ADRは実行ランタイムの製品選定を固定しない。LangGraph、DBOS、Letta等を比較する場合も、本ADRのデータ・安全・更新契約を共通の受入条件とする。

競合する範囲では次の既存ADRを部分的に改定する。

- `wave2-memory-formation-retrieval-2026-08.md`
  - 旧`EPISODIC_EVENT.subject=USER/SHARED/SELF`による経験分類は新契約では要求しない。所有characterと行為者・参加者・話題の人物を分ける。
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
保存済み会話履歴 / 許可された活動
     +-> Episode / Fact形成・登録（#340、活動ログの実抽出は#249）
     |          |
     |          +-> 保存済みEpisode + 有効なFact参照
     |                     +-> 一般化（#100）-> EXPERIENCE_DERIVED Semantic
     |                     +-> Reflection（#100）
     |                              +-> Life State（#249）
     |                              +-> Personality（#101）
     |
     +-> 明示命題の抽出・検証（#341）-> DIRECT_EXTRACTION Semantic

Life State / Personality / Memory
     -> Activity / Conversation -> 新しい経験
```

Memory、Fact、Reflection、Personality、Relationship、Skill、runtime stateは同じ正本として扱わない。
Semantic Memoryの共通Storeは#341、Episode / Factの共通基盤は#340、経験の一般化・内省は#100が所有する。

## 2. Episode / Episodic Memory

### 2.1 所有キャラクターの経験と、関連Factを分ける

Episodeは、所有するcharacter_idの視点で整理した経験の記憶である。会話・共同活動・会話外活動の場所によらない。
所有者を不明な行為者として補完せず、実際の参加者・役割と話題に登場する人物を分離する。同名だけで人物を同定しない。

Episodeの内容は5Wで表現し、必須はWhatの述語のみとする。Who / When / Where / Why / 目的語等の欠損を許容する。
Whyは明示理由のみ。保存価値・privacy・source・schema検証と、管理情報の必須性は内容の欠損許容とは別に扱う。

Related Factsはその経験で得た情報・申告内容であり、独立したfact_id付きレコードとEpisode–Fact参照で保持する。
Factには話題の対象の5Wを持たせ、Episodeの「聞いた経験」の5Wとは混同しない。
物理table・型は#342で確定し、#289の未リリース形式や旧subject分類を継承条件にしない。

| 記録例 | 区別するもの |
|---|---|
| 光織はユーザー、蒼と静岡へ行った | 光織自身の旅行経験 |
| 光織はユーザーと以前の旅行を語った | 思い出を語った経験。旅行そのものとは別Episode |
| 光織はユーザーから静岡旅行の話を聞いた | 聞いた経験と、ユーザーが旅行したというFact。光織を旅行参加者にしない |
| 蒼も同じ話を聞いた | 蒼が所有する別Episode。同じ話題でもcharacter境界を越えてFactを統合しない |

Factが同一でも、二度聞いた経験まで統合しない。元Fact・元発言/版・取得経緯を残し、確認済みの同一性は版付きID関係で表す。
2026-09-13の#340合意により、日常経験・明示的な仮定/創作の会話も文脈を保持して対象に含める。
一続きの経験はまとめ、改めて語り直した経験は新規Episode、明確な補足・訂正は既存Factの内容版更新とする。
Fact更新と5W一致によるFact統合は区別し、詳細は境界ADR第2.1節・第3.1節・第5.3節を参照する。

### 2.2 経験日時と対象日時

- Episodeのoccurred_at相当: 今回の経験が起きた日時。話を聞いた場合は聞いた日時。
- Factの対象日時: 話題の出来事が起きた日時・部分日時・可能範囲・精度。
- experienced_at: 所有キャラクターが経験を得た日時。内省・人格の時間基準。
- stated_at: 元発言日時。
- created_at: DB登録日時。

過去の旅行を今日聞いた場合、Episodeの経験日時は今日、Factの対象日時は過去とする。
対象日時不明をstated_at / created_atで埋めない。タイムゾーンはconfigで定義し、相対日時は元発言日時を基準に解釈する。
使用timezoneと精度を保持し、夜間実行・再試行・config変更で黙って再解釈しない。「先月」を月初の特定日にしない。

### 2.3 Source / provenanceと処理の境界

活動ログとEpisodeは分け、全Tool callやdomain recordをEpisodeへ変換しない。元resource / activityへのprovenanceを保持する。

```text
Activity / Tool Execution
  -> #249: 活動ログの要約・保存条件・意味評価
  -> #340: 共通Episode / Fact登録契約
```

会話由来の抽出は#291がスレッド更新を起点に非同期予約し、#290が同一character・同一threadのFactを抽出・登録時に照合する。
5Wと文脈で同一出来事への再言及と確認できた場合だけIDでまとめる。unknown同士・日時包含・同名だけを一致根拠にしない。
会話応答は完了を待たず、同thread照合を別の夜間処理の完了待ちにも置かない。
別thread間の保存済みFact統合は#354の後続非同期整理であり、#340 / #341 / #100のMVP依存にしない。

## 3. Semantic Memory / Reflection / Candidate

### 3.1 Semantic Memory

Semantic Memoryは、世界・人物・自分について知識として採用する事実・概念・傾向である。
Episodeからの一般化だけを意味記憶全体の定義にしない。

- DIRECT_EXTRACTION: #341が明示命題を発言・許可された情報源から抽出する。複数Episodeを要求しない。
- EXPERIENCE_DERIVED: #100が保存済みの独立した複数Episodeから一般化する。感情・意思・自己評価を事実的根拠に混ぜない。

formation_typeは形成方法、provenanceは実際の情報源・各参照元の版である。共通schema・保存・訂正・検索は#341が所有する。
「客観的な一般化」は外部世界の絶対的真実ではなく、入力経験へ根拠を追跡できる共通要素を意味する。
Factは付随情報であってSemantic正本ではなく、Fact保存・統合だけで自動昇格しない。
元Episodeを削除・置換せず、派生元とその版をlineage / provenanceで保持する。

### 3.2 Reflection

Reflectionは、本人が複数のEpisodeをどう認知・評価し、何を感じ、何を大切にし、今後どうしたいと捉えたかを表す。

ReflectionはSemantic Memoryとは別概念であり、#292が別の永続model/tableで正本を保持する。同じpersona-memory.dbへ保存してよい。
MVP lifecycleはACTIVE / SUPERSEDED / INACTIVEとする。

再内省により同じ論点の捉え方が変わった場合、旧ReflectionをSUPERSEDEDとして残し、新ReflectionをACTIVEにする。
異なる観点のReflectionは共存できる。新しい関連Episodeや根拠訂正/削除で再評価し、Personality変更だけでは再内省しない。
実行時は現在のPersonalityを利用できる。旧Reflectionは補助情報であり、独立Episode根拠には数えない。

Reflectionは自由文とprovenanceを正本とし、Insight / Interest / Intentionを同じ自由文へ埋め込んで更新し続けない。
**通常会話のRAG / promptへReflectionを直接注入しない。** 例外的な自己説明検索は今回追加しない。

### 3.3 Reflectionからの派生

Reflectionから次の概念を分離して派生可能とする。

- Insight: 自己洞察。「自分はどういう傾向・価値を持つようだ」と理解した内容。
- Current Interests: 現在注意・好奇心が向いている対象。
- Goal Intention: 今後どうしたいかという目標意図。
- Implementation Intention: 「XならYする」という条件付きの実行意図。

Reflectionは過去の認知、Intentionは未来の方向・行動方針である。
#100がprovenance・validity付きの検証済み結果を形成し、Life Stateとしての運用は#249、人格更新は#101が行う。

### 3.4 Candidate

Candidateは、LLM等が生成したが、privacy・evidence・schema・policy検証前のデータである。
検証を通った候補だけを自動保存・有効化し、手動候補承認は追加しない。
未検証/拒否Candidateを検索・人格更新へ使用しない。拒否本文は保存せず、許可された監査情報だけを残す。

Semantic / Reflectionは、派生元Episodeが保存可能であってもprivacyを再評価する。
複数の安全なEpisodeから機微情報を推論する可能性があるためである。Factや統合関係にもsource・privacy境界を適用する。

## 4. Episode bundle / cluster

Semantic Abstraction / Reflection形成時のclusterは永続entityではなく、今回一緒に評価するEpisode集合とする。

1. 新規・更新Episode等をanchorにする。
2. Chromaで近傍候補を取得する。
3. SQLite正本でcharacter、型、world、人物・役割、時刻・精度、sourceの版、Fact参照・統合関係の有効性を確認する。
4. 独立した2件以上の経験を最低条件とし、支持・反証・適用条件を評価する。不足・矛盾があれば条件付き一般化または見送りとする。

character_idは絶対境界。timeは検索範囲・関連性・継続性に使い、機械的な同日clusterへ分断しない。
候補件数・類似度threshold・time windowは設定可能にする。気質/人格による着目は許容するが事実根拠を水増ししない。

同じ有効な代表Fact IDを同一の話題の出来事について二重加算しない。
異なるFact / Episode / thread / 発言日時は独立性の証明ではない。独立性不明の組は独立2件の根拠に使わず、同一だと強制統合もしない。
同じ旅行について二度聞いた場合、聞いた経験は二件でも、二回の旅行の証拠にはしない。

#295はMVPでは夜間固定時刻に一般化・Reflection形成/再評価を行う。
#291の抽出・登録、#341のDirect Extraction、#354の別thread整理とはトリガーを分ける。

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

元Episode / Fact / source / 統合関係の訂正・削除・失効時、依存するSemantic / Reflectionと派生結果を
SQLite上で即時利用停止し、再評価対象にする。物理削除や人格全体の無条件rollbackとは分離する。
Life StateとPersonalityは既存の正本境界で残存根拠・影響範囲から再評価する。
古いindex・参照・runtime checkpointから削除済み情報を正本へ復活させない。
詳細は[参照失効契約](episode-fact-semantic-boundaries-2026-09.md)を適用する。

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

少なくとも次を検証する。各機能の受入を本ADRの更新完了と同一視しない。

1. #340でEpisode / FactをID・出典付きで保存し、同thread登録時の5W一致確認と見送りを検証する。
2. #341でDIRECT_EXTRACTIONとEXPERIENCE_DERIVEDを共通Semanticとして管理し、#100で一般化・Reflection形成を検証する。
3. 本人の会話外活動は#249が共通契約を利用し、所有characterのEpisodeとして保存する。旧SELF分類は必須にしない。
4. 同じEpisodeや同じ旅行Factについて再内省してもPersonality evidenceが水増しされない。
5. 反証がある場合はPersonality更新を弱化・保留できる。
6. Big Five Aspects 10因子の更新がboundedで監査可能である。
7. Egogram評価は表出一貫性の質的評価としてのみ使われる。
8. Relationship 2軸とInterpersonal Skillが分離される。
9. 許可済みMCPを自律利用でき、通常のupdateも実行できる。
10. Minimum Disclosure後にも機微情報を意味推論したargumentはBLOCKされる。
11. 操作群・実行場面別の承認を評価し、初期状態ではHigh Impactが#185確認へ流れる。通常のexternal-sendを一律に停止せず、保存済み承認も適用する。
12. MCP Tool変更は次snapshotから反映され、Tool単位の再承認を要求しない。
13. 根拠訂正・削除後の即時利用停止と再評価を確認し、古いcheckpointや参照から復活させない。
14. foreground優先を維持しつつ、background preemption要否を性能評価できる。
15. UIは#343 / #348 / #351、実接続受入は#344 / #349 / #297で各Epicごとに確認する。#354の別thread整理をMVP完了条件にしない。
