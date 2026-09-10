# 記憶・内省・人格・会話利用の用語契約 (2026-09)

## 状態

**ACTIVE**。

本ADRは、#100 / #101 / #102 / #103 および関連する記憶・人格実装で使用する用語と概念境界を固定する。

実装方式そのものを確定するADRではなく、既存の `character-life-memory-personality-autonomy-2026-09.md` を補助する共通語彙の正本とする。既存ADRの実装契約と競合する場合は、実装契約側を優先し、本ADRは用語解釈を与える。

## 1. 基本構造

キャラクターの経験から人格変化、会話時の利用までを次の流れとして扱う。

```text
Experience
   ↓
Memory Formation
   ↓
Episodic Memory
   │
   ├─ Memory Consolidation
   │
   ├─ Semantic Abstraction
   │      ↓
   │   Semantic Memory
   │
   └─ Reflection
          ├─ Current Interests Update
          │
          └─ Personality Adaptation
                 ↓
              Personality

Execution Log
   ↓
Procedural Learning
   ↓
Procedural Memory / Skill
```

人格そのものを記憶の一種として扱わない。

```text
Memory
= 何を経験し、知り、解釈し、身につけたか

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

例:

- 慎重さ
- 積極性
- 自信
- 好奇心
- 対人距離

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

会話生成時は原則として常時参照する。

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

### 3.3 Long-term Memory / 長期記憶

セッションを超えて保持される記憶の総称。

概念上、次を含む。

```text
Long-term Memory
├─ Episodic Memory
├─ Semantic Memory
├─ Reflective Memory
└─ Procedural Memory
```

保存方式や正本は記憶種別ごとに異なってよい。同一table / databaseに格納されることを意味しない。

### 3.4 Episodic Memory / エピソード記憶

所有する`character_id`の視点で「何を経験したか」を表す具体的な経験の記憶。
経験の主語は所有キャラクターに固定し、交流相手・参加者・話題の人物は役割を区別して保持する。
会話か外部サイトかによらず同じ契約で扱い、旧USER / SHARED / SELFというsubject分類は使わない。

例:

- ユーザーと一緒に問題を解決した
- キャラクター自身が外部活動で何かを観測した
- ある出来事についてユーザーから初めて聞いた

特定の時刻・文脈・所有者と結びつく。旅行した経験とその思い出を語った経験は別Episodeであり、
話を聞いた経験の発生日時は「聞いた日時」、話題の出来事の日時は任意の関連情報である。会話中は関連する過去経験として必要時にretrieveする。

### 3.5 Semantic Memory / 意味記憶

特定の1つのEpisodeそのものではなく、事実・概念・安定した傾向・一般化された知識を保持する記憶。

現在の直接形成memoryとして `USER_PREFERENCE` / `INTERACTION_PREFERENCE` が存在する。

将来的には複数EpisodeからSemantic AbstractionによってDerived Semantic Memoryを形成する。

```text
Episode A
Episode B
Episode C
   ↓
Semantic Abstraction
   ↓
Derived Semantic Memory
```

一般化のために元Episodeを削除・置換しない。派生元をlineage / provenanceで追跡可能にする。

会話中は必要時にretrieveする。

### 3.6 Reflective Memory / 内省記憶

Reflectionの結果を永続化した記憶。

Reflectionは、複数の経験について本人が「どう認知したか」「どう感じたか」「何を学んだか」「何を大切にしたいか」を自己解釈した結果である。

Semantic Memoryとは区別する。

```text
Semantic Memory
= 何が一般的に成立していると捉えたか

Reflection
= その経験を本人がどう意味づけたか
```

Reflectionは通常会話のRAG対象として**原則直接参照しない**。

主な用途は以下とする。

- Current Interests更新
- Personality Adaptation
- Goal / Intention等の自己状態の形成
- 自己説明・内省回想が明示的に必要な場合の根拠

通常会話でReflectionとPersonalityの双方を直接注入し、同一傾向を二重に効かせない。

### 3.7 Procedural Memory / 手続き記憶

「何を知っているか」ではなく「どう実行するか」を保持する記憶。

AIではSkill / procedureとして表現する。

主入力はEpisodeの内省ではなく、実際の実行履歴とその結果とする。

```text
Execution Log
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

会話生成の一般RAG対象ではなく、tool / addon / game等の実行時に参照する。

## 4. Reflectionから派生する自己状態

### 4.1 Current Interests / 現在の興味関心

現在、何に注意・関心・好奇心が向いているかを表す短〜中期の可変自己状態。

人格そのものではなく、Reflectionや最近のEpisode、継続的な関与から更新される。

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

Current InterestsはReflective Memoryそのものではなく、**Reflectionから派生・更新される自己状態**として扱う。

## 5. 処理用語

### 5.1 Memory Consolidation / 記憶統合

既存記憶の整理・正規化・更新を行う処理。

#48で実装済みの `KEEP / MERGE / SUPERSEDE / DELETE_EXACT_DUPLICATE / CONFLICT / NOOP` を代表例とする。

同種記憶の整理を主責務とし、Episodeから別種のSemantic MemoryやPersonalityを直接生成する処理ではない。

### 5.2 Semantic Abstraction / 意味抽象化

複数のEpisodeから共通する安定したパターン・知識を抽出し、Derived Semantic Memoryを生成する処理。

#100で扱う。

Memory Consolidationとは分離する。

### 5.3 Reflection / 内省

複数のEpisodeを本人視点で認知・評価し、自己解釈を形成する処理、およびその結果を指す。

文脈上曖昧になる場合は、処理を `Reflection Process`、永続化された結果を `Reflective Memory` と呼ぶ。

#100で扱う。

### 5.4 Personality Adaptation / 人格適応

複数のReflectionとその根拠Episodeから長期的な傾向を評価し、変動パーソナリティであるPersonalityを緩やかに更新する処理。

#101で扱う。

単一Episodeから直接Personality Deltaを適用しない。

### 5.5 Procedural Learning / 手続き学習

実行ログ、成功・失敗、修正等のフィードバックからSkillを生成・更新し、Procedural Memoryとして再利用可能にする処理。

#102で扱う。

### 5.6 Forgetting / 忘却

長期記憶の通常の忘却を、記憶内容の物理削除ではなく**retrievabilityの低下**として扱う概念。

```text
Forgetting
≠ memory deletion

Forgetting
= reduced retrieval accessibility
```

SQLite等の正本を原則保持し、Chroma等のsemantic index、structured / temporal retrieval、ranking、retrieval policyを通じて想起しにくさを表現する。

強いsemantic / temporal / contextual cueがある場合は再想起可能なモデルを想定する。

#103で扱うが、現時点では保留し、必要性が確認できた段階で詳細化・実装する。

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
├─ Episodic Memory
└─ Semantic Memory

通常会話では原則直接参照しない
└─ Reflective Memory

実行時に参照
└─ Procedural Memory / Skill
```

Reflectionを通常会話へ直接注入せず、Current InterestsやPersonalityへ変換された状態を通じて会話へ反映する。

自己説明や「なぜそう考えるようになったか」を問われる特殊な経路では、概念上はReflectionや
provenanceを明示的に参照できる。ただし#100ではこの例外経路を追加せず、通常会話への直接注入を行わない。

## 7. 概念階層

```text
Character State
│
├─ Temperament / 気質
│   └─ 不変パーソナリティ
│
├─ Personality / 人格
│   └─ 変動パーソナリティ
│
├─ Current Interests / 現在の興味関心
│   └─ 短〜中期の可変自己状態
│
└─ Memory
    ├─ Working Memory
    ├─ Conversation History
    └─ Long-term Memory
        ├─ Episodic Memory
        ├─ Semantic Memory
        ├─ Reflective Memory
        └─ Procedural Memory
```

## 8. 関連Epic / ADR

- #48: Memory Consolidation。既存memoryの同種整理。
- #100: Episode共通契約と会話由来抽出の改修、Semantic Abstraction / Reflectionと検証済み派生結果。
- #249: 会話外活動ログからのEpisode生成、Life State運用、実行管理。
- #101: Reflection群からのPersonality Adaptation。
- #102: execution logからのProcedural Learning / Skill更新。
- #103: forgettingをretrievability低下として扱う将来検討。
- `character-life-memory-personality-autonomy-2026-09.md`: Episode / Reflection / Life State / Mutable Personality / Autonomy等の実装契約。
- `wave2-memory-formation-retrieval-2026-08.md`: 現行persona memory形成・retrievalの基盤契約。

## 9. 非目標

本ADRでは以下を確定しない。

- #100〜#103の具体schema
- scheduler / batch / threshold等の具体値
- Personality Delta計算式
- Reflectionのprompt
- Skillファイル形式
- retrievabilityの具体的ranking式
- Current Interestsの減衰率

これらは各Epicおよび後続ADR / 子Issueで確定する。
