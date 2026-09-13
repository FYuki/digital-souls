# 記憶・内省・人格・会話利用の用語契約 (2026-09)

## 状態

**ACTIVE**。

本ADRは、#340 / #341 / #100 / #101 / #102 / #103 / #354および関連する記憶・人格実装で使用する用語と概念境界を固定する。

用語の正本として[Character Life共通契約](character-life-memory-personality-autonomy-2026-09.md)を補助する。
2026-09-12の#352による記憶責務再編、FactのID参照・照合・失効については
[Episode・Fact・Semanticの正本と形成責務](episode-fact-semantic-boundaries-2026-09.md)を優先する。
ACTIVEは採用設計を意味し、以下の新モデルがruntimeに実装済みであることを意味しない。

## 1. 基本構造

キャラクターの経験・情報取得から人格変化、会話時の利用までを次の流れとして扱う。

```text
保存済み会話 / 許可された活動
  +-> Episode / Fact Formation（#340、会話外ログの実抽出は#249）
  |        ↓
  |     Episodic Memory -- ID参照 -- Related Facts
  |        +-> Semantic Abstraction（#100）-> EXPERIENCE_DERIVED Semantic
  |        +-> Reflection（#100）
  |                  +-> Current Interests / Intention（#249で運用）
  |                  +-> Personality Adaptation（#101）-> Personality
  |
  +-> Direct Semantic Extraction（#341）-> DIRECT_EXTRACTION Semantic

同一character・別threadの保存済みFact
  -> Cross-thread Fact Consolidation（#354、後続非同期処理）

Execution / Interaction Log
  -> Procedural Learning（#102）-> Procedural Memory / Skill
```

人格そのものを記憶の一種として扱わない。

```text
Memory
= 経験・知識・保存された解釈・身につけた手続き

Personality
= それらの蓄積を背景に形成された持続的な傾向
```

## 2. パーソナリティ用語

### 2.1 Temperament / 気質

**不変パーソナリティ**を指す。

経験や内省によって変更しない、キャラクターの核となる性質・価値観・制約である。

例:

- 素直である
- 他者と協力的である
- 嘘をつかない
- 他者を尊重する

会話生成時は原則として常時参照し、変動する人格より上位の制約として作用させる。

### 2.2 Personality / 人格

**変動パーソナリティ**を指す。

複数のReflectionや長期的な経験傾向から、Personality Adaptationを通じて緩やかに変化する。
正本は#101のBig Five Aspects 10因子であり、例示語をそのまま追加traitにはしない。
特定相手との対人距離はRelationship State、対人行動の実行能力はInterpersonal Skillとして分離する。

例:

- 慎重さ
- 積極性
- 自信
- 好奇心
- 計画性

単一Episodeから直接変更しない。

```text
Episode群
  ↓
Reflection群
  ↓
長期傾向評価
  ↓
Personality Adaptation
  ↓
Personality
```

会話生成時は原則として常時参照する。Egogramは評価指標であり、人格の正本や注入コンテキストへ戻さない。

## 3. 記憶用語

### 3.1 Working Memory / 作業記憶

現在の課題を処理するために一時的に保持・操作する情報。

- current input
- current goal
- tool / addon実行中の状態
- 現在の推論・処理コンテキスト

基本的に長期記憶として永続化しない。会話中は常時参照する。

### 3.2 Conversation History / 会話履歴

現在の会話の連続性を維持するための履歴。

長期的なpersona memoryとは区別する。会話時は直近文脈として優先的に参照する。
#291のEpisode / Fact抽出では保存許可されたスレッドsnapshotを使うが、会話履歴全体をそのまま長期記憶にはしない。

### 3.3 Long-term Memory / 長期記憶

セッションを超えて保持される記憶の総称。

本プロジェクトの概念上、次を含む。

```text
Long-term Memory
├─ Episodic Memory（Related FactsをID参照）
├─ Semantic Memory
├─ Reflective Memory
└─ Procedural Memory
```

保存方式や正本は記憶種別ごとに異なってよい。同一table / databaseに格納されることを意味しない。
この分類はプロジェクト用の概念整理であり、Reflective MemoryやFactを学術的な独立記憶システムと主張するものではない。

### 3.4 Episodic Memory / エピソード記憶

「所有キャラクターが何を経験したか」という具体的な経験の記憶。#340が正本と形成・登録を所有する。

例:

- ユーザーと一緒に問題を解決した
- キャラクター自身が外部活動で何かを観測した
- ある出来事についてユーザーから初めて聞いた

経験の5Wを保持するが、内容上の必須はWhatの述語だけである。他の5Wが不明でも保存条件を満たせば保持できる。
character_idなどの管理情報は別に必須。所有者を不明な行為者として補わない。
会話中は関連する過去経験として必要時にretrieveする。
2026-09-13の定義補足: 一続きの経験は複数発言・抽出実行をまたいでも1 Episodeとする。
後から改めて語り直した「聞いた経験」は新規Episodeであり、話題のFact情報の更新とは区別する。
日常経験、明示的な仮定・創作を話した経験も対象に含む。仮定・創作内の出来事を実体験と混同しない。

### 3.4.1 Fact / Related Facts / 関連事実

Episodeの中で得た、話題の対象についての情報・申告内容。外部世界で検証済みの真実やSemanticの正本ではない。

```text
Episode:
光織は9月12日にユーザーから旅行の話を聞いた

Fact:
ユーザーは9月11日に静岡へ行ったと述べた
```

Episodeの経験日時とFactの対象日時を分離する。Whyは明示理由のみとし、推論を事実化しない。
Related Factsは意味上はEpisodeに付随するが、独立したfact_id付きレコードとして持つ。
EpisodeからID参照し、JSON配列だけに閉じ込めない。API / UIで配列へ投影することは許容する。
Factの保存・統合はSemanticへの自動昇格ではなく、必要な知識化は別の抽出・検証を通す。

補足・訂正によって同じ対象の情報が変わる場合、対象が明確な既存Factの内容版を更新する。
改めて語り直した経験は新規Episodeから参照し、Factの変更前後の版と各取得元を追跡する。
内容が変わらない再言及ではFact本文の更新は不要。仮定・創作の情報にはその文脈を保持する。

### 3.4.2 Episode–Fact Reference / Fact Merge / 代表Fact ID

Episode–Fact参照は、どの経験でどのFactを取得したかを表し、元発言ID・各版・入力範囲・有効性を保持する。
Fact統合関係は、元Fact IDから統合先IDへの版付き同一性関係であり、判定根拠・scope・有効性を追跡する。

代表Fact IDは、有効な関係を解決したときの同一Factの識別子である。現実世界の出来事の絶対的なIDではない。
Fact IDを5Wのhashにせず、元Episode・元Fact・取得経緯を消さない。Fact統合でEpisodeを統合しない。
異なるFact IDは独立した出来事の証明ではない。循環・複数の有効統合先・別character参照・無効内容の復活を許可しない。

### 3.5 Semantic Memory / 意味記憶

特定の1つのEpisodeそのものではなく、世界・人物・自分について採用した事実・概念・傾向・一般化知識を保持する記憶。
共通正本・保存・訂正・検索は#341が所有する。

現在の直接形成memoryとしてUSER_PREFERENCE / INTERACTION_PREFERENCEが存在する。
これは新しい汎用Semantic schemaや形成経路フィールドが実装済みであるという意味ではない。
新設計では次の2経路を明示する。

```text
保存可能な明示命題
  -> Direct Semantic Extraction（#341）
  -> formation_type = DIRECT_EXTRACTION

独立した保存済みEpisode群
  -> Semantic Abstraction（#100）
  -> formation_type = EXPERIENCE_DERIVED
```

DIRECT_EXTRACTIONは複数Episodeを要求しないが、単一観測からの傾向推測を明示事実として確定しない。
EXPERIENCE_DERIVEDは独立した2件以上を最低条件とし、不足・矛盾・独立性不明なら見送る。
一般化のために元Episodeを削除・置換しない。会話中は必要時にretrieveする。

### 3.5.1 Formation Type / Provenance / Source Revision

formation_typeはどう形成したか、provenanceは何を情報源・根拠にしたかを表す。
source revisionは根拠のどの版を使ったかであり、各参照元に対応づける。

同じ元発言をEpisode / Fact / Semanticとして持っても、独立した根拠が増えたとは数えない。
旧formation_methodのEXTRACTED / CONSOLIDATED等を、新しい獲得経路と同一視しない。

### 3.6 Reflective Memory / 内省記憶

Reflectionの結果を永続化した記録。#100 / #292がSemanticと別の正本として管理する。
本人が複数の経験についてどう認知・評価し、何を感じ、何を大切にし、今後どうしたいかを自己解釈した結果である。

```text
Semantic Memory
= 情報源や経験を根拠に、何を知識として採用したか

Reflection
= 経験を本人がどう意味づけたか
```

Reflectionは通常会話のRAG / promptへ直接注入しない。
主な用途はCurrent Interests、Goal / Intention等の自己状態形成とPersonality Adaptationである。
自己説明・内省回想のための例外的検索は将来検討であり、今回のMVPには追加しない。

ACTIVE / SUPERSEDED / INACTIVE、根拠・版・同論点の再内省履歴を保持する。異なる観点は共存できる。
旧Reflectionは補助情報であり独立した経験として加算しない。新しい関連経験や根拠訂正/削除で再評価する。
Personality変更だけをMVPの再内省トリガーにしない。

### 3.7 Procedural Memory / 手続き記憶

「何を知っているか」ではなく「どう実行するか」を保持する記憶。
AIではSkill / procedureとして表現する。主入力はEpisodeの内省ではなく、実際の実行・対話履歴とその結果とする。

```text
Execution / Interaction Log
├─ action sequence
├─ success / failure
├─ retry
├─ workaround
└─ user correction
        ↓
Procedural Learning
        ↓
Skill create / update
        ↓
Procedural Memory
```

会話生成の一般RAG対象ではなく、tool / addon / game / interaction等の実行時に参照する。
FactのID統合やReflectionだけでSkillを確定する処理ではない。

## 4. Reflectionから派生する自己状態

### 4.1 Current Interests / 現在の興味関心

現在、何に注意・関心・好奇心が向いているかを表す短〜中期の可変自己状態。
人格そのものではなく、Reflectionや最近のEpisode、継続的な関与を根拠に更新される。

```text
最近のEpisode群
   ↓
Reflection
   ↓
Current Interests
```

例:

```text
Personality:
好奇心が高い

Current Interests:
現在はLive2Dモデリングへの関心が高い
```

Personalityより変動しやすく、Working Memoryより長く持続する。
会話では話題選択、質問、自発的な関心表明などに利用するため、常時または軽量なcharacter contextとして参照可能とする。
Current InterestsはReflective Memoryそのものではなく、Reflectionから派生・更新される自己状態である。
検証済み結果の形成は#100、Life Stateとしての運用は#249が所有する。

## 5. 処理用語

### 5.1 Memory Consolidation / 記憶整理・統合

既存記憶の整理・正規化・更新を行う処理。
#48のKEEP / MERGE / SUPERSEDE / DELETE_EXACT_DUPLICATE / CONFLICT / NOOPを代表例とする。
同種記憶の整理が主責務で、Episodeから別種のSemanticやPersonalityを直接生成する処理ではない。

### 5.1.1 In-thread Fact Matching / 同一スレッド内Fact照合

同一character・同一threadの会話履歴からFactを抽出・登録するときに、今回の候補同士と同threadの保存済みFactを照合する処理。
#290が判定・登録、#291が非同期抽出・呼出しを所有する。

Factの5Wと文脈から同一出来事への再言及と確認できた場合のみ、IDの紐付けで統合する。
unknown同士・同名だけ・日/月範囲の重なり・同日別回の可能性は一致の根拠にしない。
会話応答に同期する処理でも、別の夜間整理待ちでもない。同一入力の再試行による二重保存防止は別の制御である。

### 5.1.2 Cross-thread Fact Consolidation / 別スレッド間Fact整理

#354が、同一character・異なるthreadに出典を持つ保存済みFactを、登録経路とは別の非同期処理で整理・統合する。
重複Episode整理と同様の処理モデルだが、統合対象はFactの同一性でありEpisodeそのものではない。
後続拡張として扱い、MVPの#340 / #341 / #100をその完了待ちにしない。別character間の統合は禁止する。

### 5.1.3 Fact Update / Fact情報の更新

同一character・同一threadで対象が明確な補足・訂正、または管理UIのFact単位の訂正により、
安定したfact_idに対応する内容版と根拠を更新する処理。
別々のFactの同一性を確定するFact Mergeとは異なり、変更前後の5W一致を条件にしない。
曖昧な対象の推測上書きや、根拠のない日時・動機の補完は含まない。
詳細は[境界ADR](episode-fact-semantic-boundaries-2026-09.md)の第5.3節・第9節を参照する。

### 5.2 Semantic Abstraction / 意味抽象化

保存済みの複数Episodeから共通するパターン・知識を抽出し、EXPERIENCE_DERIVED Semanticを生成する処理。
#100で扱い、Memory ConsolidationやFact統合とは分離する。
有効な代表Fact IDを使って同じ話題の出来事の重複加算を防ぎ、異なるIDだけで独立性を認めない。

### 5.2.1 Direct Semantic Extraction / 意味知識の直接抽出

保存可能な発言・許可された情報源から明示された命題を抽出・検証し、DIRECT_EXTRACTION Semanticとして採用する処理。
#341が所有する。一度の明示発言から候補化できるが、privacy・出典・版・矛盾の検証を省略しない。

### 5.3 Reflection / 内省

複数のEpisodeを本人視点で認知・評価し、自己解釈を形成する処理、およびその結果を指す。
曖昧になる場合は処理をReflection Process、永続化された結果をReflective Memoryと呼ぶ。#100で扱う。

### 5.4 Personality Adaptation / 人格適応

複数のReflectionと根拠Episodeから長期傾向を評価し、変動パーソナリティを緩やかに更新する処理。
#101で扱い、単一Episodeから直接Personality Deltaを適用しない。

### 5.5 Procedural Learning / 手続き学習

実行ログ、成功・失敗、修正等のフィードバックからSkillを生成・更新し、Procedural Memoryとして再利用可能にする処理。
#102で扱う。

### 5.6 Forgetting / 忘却

長期記憶の通常の忘却を、記憶内容の物理削除ではなくretrievabilityの低下として扱う概念。

```text
Forgetting ≠ memory deletion
Forgetting = reduced retrieval accessibility
```

SQLite等の正本を原則保持し、Chroma等のindex、structured / temporal retrieval、rankingを通じて想起しにくさを表現する。
強いsemantic / temporal / contextual cueがある場合は再想起可能なモデルを想定する。
#103で扱うが、必要性が確認できた段階で詳細化・実装する。

### 5.7 Invalidation / 利用停止・再評価

根拠Episode / Fact / source / 統合関係の訂正・削除・版失効により、依存する結果や参照を利用停止する処理。
曖昧な同一性の統合、通常の忘却、人格全体のrollback、物理削除とは区別する。
古いChroma・代表ID・統合履歴から無効内容を復活させず、SQLiteと参照先の有効性を確認して残存根拠から再評価する。

## 6. 会話・実行時の参照契約

概念上の既定値を次とする。

```text
常時またはcharacter contextとして参照
├─ Temperament / 気質
├─ Personality / 人格
├─ Current Interests / 現在の興味関心
├─ Working Memory
└─ Conversation History

必要時にretrieve
├─ Episodic Memory（有効なFact参照を含む経験文脈）
└─ Semantic Memory

通常会話へ直接注入しない
└─ Reflective Memory

実行時に参照
└─ Procedural Memory / Skill
```

ReflectionはCurrent InterestsやPersonalityへ変換された状態を通じて会話へ反映する。
例外的な自己説明検索はMVPに含めない。Factを読めることと、独立したSemanticとして採用済みであることは別である。

## 7. 概念階層

```text
Character State
├─ Temperament / 気質（不変）
├─ Personality / 人格（可変の持続的傾向）
├─ Life State（Current Interests / Intention等）
├─ Relationship State（特定相手との関係）
└─ Memory
    ├─ Working Memory
    ├─ Conversation History
    └─ Long-term Memory
        ├─ Episodic Memory -- ID参照 -- Related Facts
        ├─ Semantic Memory（DIRECT_EXTRACTION / EXPERIENCE_DERIVED）
        ├─ Reflective Memory
        └─ Procedural Memory
```

## 8. 関連Epic / ADR

- #340: Episode / Fact正本、非同期抽出、同thread登録時照合。
- #341: Semantic正本・Direct Extraction・訂正・検索。
- #354: 別thread間Factの後続非同期整理。MVPの必須依存ではない。
- #48: 既存memoryの同種整理。新Fact統合の実装済み証拠とはしない。
- #100: Semantic Abstraction / Reflection形成・再内省。
- #101: Reflection群からのPersonality Adaptation。
- #102: 実行・対話ログからのProcedural Learning / Skill更新。
- #103: retrievability低下としての忘却の将来検討。
- #249: Life Stateと会話外活動runtime。
- [Episode・Fact・Semantic境界ADR](episode-fact-semantic-boundaries-2026-09.md): 記憶正本・ID参照・照合・失効の詳細。
- [Character Life共通ADR](character-life-memory-personality-autonomy-2026-09.md): Life State・人格・自律活動との接続。
- [Wave 2 ADR](wave2-memory-formation-retrieval-2026-08.md): 継続する保存・privacy・retrieval基盤。

## 9. 非目標

本ADRでは具体的な物理schema、scheduler / batch / threshold値、Personality Delta式、Reflection prompt、Skill形式、
retrievabilityのranking式、Current Interestsの減衰率を確定しない。各Epic・後続設計で定める。
#289の未リリース旧形式を新モデルの制約にせず、文書上の採用と実装・main取り込み・dogfood反映を区別する。
