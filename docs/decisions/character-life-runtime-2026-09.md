# Character Life Runtime選定（2026-09）

## 状態

**ACTIVE**。

#249のCharacter Life Runtimeについて、LangGraph / DBOS / Lettaの責務と統合方式を比較し、MVPのtop-level runtimeとしてDBOSを採用する。本ADRは採用方針を定めるものであり、実装・実接続受入の完了証跡ではない。

人格・Memory・Reflection・Life State・Autonomyの意味契約は
`character-life-memory-personality-autonomy-2026-09.md`を正本とする。本ADRはそれらの意味を変更せず、会話外処理をいつ・どのように継続実行するかだけを決める。

## 背景

Character Life Runtimeへ任せたい責務は次である。

- background workflowの起動
- queue / concurrency control
- schedule
- wait / resume
- process restart後のrecovery
- 実行状態の追跡
- foreground / background priorityを実装するための実行境界

一方、次はruntimeへ委譲しない。

- SQLite / Chromaのpersona memory正本
- Reflection / Life State / Personalityの意味と更新条件
- privacy / Minimum Disclosure / Egress Check
- #104 Execution Gate
- #185 High Impact / ActionRecovery
- 外部操作のresult_unknown判定

## 実装時の検証シナリオ

採用後の実装では、Episode → Reflection → Life State → 活動というdomain境界を維持する。
認知品質の評価と、DBOSによる実行制御の受入は分ける。

- 通常実行、同じworkflow IDの再投入、途中停止・再起動後の復旧
- 完了済みstepの再利用と、domain正本更新の冪等性
- queueの同時実行制限、schedule停止・再開、missed runの非backfill
- 実際のdomain contractの受け渡しと永続化
- dispatch直前のprivacy・許可・Capability Snapshot検証

比較用コードは本実装の土台として引き継がない。共通contract、runtime、テスト、依存packageは、
既存serviceと正本DBへ接続する実装で改めて定義する。

## 比較

| 観点 | LangGraph | DBOS | Letta |
|---|---|---|---|
| Python 3.12 / FastAPIとの統合 | 良い | 良い | Python API clientはあるが現行Agent SDKはPython非対応 |
| 既存Python serviceの維持 | 可能。Functional APIなら変更を抑えられる | 非常に容易。workflow/stepとして包める | Memory/Agent runtimeを広く採用すると既存境界との重複が大きい |
| durable state | checkpointerで提供 | workflow/step system DBで提供 | Letta側Agent stateで提供 |
| SQLite local | 別checkpoint packageで可能 | 標準で可能 | self-hosted runtimeとの別境界になる |
| queue / concurrency | OSS LangGraph単体の中心機能ではない | runtime標準機能 | Agent runtime側へ依存 |
| schedule | OSS組み込みでは別実装/別deploymentが必要 | DB保存scheduleを標準提供 | schedule可能だがAgent/Memory runtimeごと採用する範囲が広い |
| 複雑なLLM分岐の表現 | 最も強い | 通常Python制御で表現 | Agent中心で表現 |
| 現行Memory正本との重複 | 小 | 最小 | 大 |
| #182置換の必要 | なし | なし | 広く採用する場合は設計変更が大きい |

## 決定

### 1. top-level Character Life RuntimeはDBOS

MVPではDBOSを次の責務に限定して採用する。

```text
DBOS
├─ durable workflow
├─ queue / concurrency
├─ schedule
├─ wait / resume / recovery
└─ workflow identity / status

Digital Souls domain
├─ Episode / Reflection / Life State
├─ Personality / Relationship
├─ privacy / Egress
├─ Tool Catalog / Execution Gate
└─ High Impact / ActionRecovery
```

DBOS system DBはworkflow状態の正本であり、persona memoryの正本にはしない。

ローカルMVPではSQLiteを利用可能とする。複数hostへ分散する必要が出た時点でPostgreSQLへ移行を検討する。

### 2. scheduleのmissed runを「不在中に活動したこと」にしない

Autonomous Activityのscheduleは原則`automatic_backfill=false`とする。

プロセス停止中に実行できなかった散策・閲覧・交流を、再起動後に過去時刻へ遡って実行し「実際に行った経験」として扱わない。

一方、未処理EpisodeのReflection等は現在の正本をscanしてcatch-up可能とする。過去のcron fire回数を再現するのではなく、未処理データを現在時点で処理する。

### 3. workflow IDをidempotency境界に利用する

Character Life workflowにはdigital-souls側で安定したworkflow IDを与える。

ただし、DBOSのworkflow再実行防止を、外部MCP副作用のexactly-once保証とは扱わない。
外部操作は引き続き#185のidempotency / status lookup / result_unknown契約を利用する。

### 4. queueはforeground優先制御の実装点にする

MVPではbackground Character Life処理の同時実行を制限できるqueueを用意する。

```text
foreground conversation
  > user-requested activity
  > autonomous background activity
```

この優先順位は維持するが、実行中backgroundを強制preemptするかはGPU/VRAM/TTFT/音声stream/複数キャラクター/配信への影響測定後に決める。

### 5. LangGraphはtop-level runtimeとして採用しない

LangGraphのstateful graph、interrupt、checkpointerは有用だが、今回最も委譲したいqueue / schedule / recoveryを一つのPython runtimeでまとめる目的にはDBOSの方が適合する。

将来、Reflection形成やAutonomous Activity内で分岐・反復が複雑になり、通常Python control flowでは可読性が低下した場合は、**DBOS workflow内の認知処理実装**としてLangGraphを再評価してよい。

その場合も、DBOSとLangGraphの双方に同じretry / resume責務を持たせない。node/stepの境界と正本を別ADRで確定してから追加する。

### 6. LettaはMVPで採用しない

Lettaのmemory-centric Agent設計は目的自体には近いが、digital-soulsでは既に次を独自契約として正本化している。

- SQLite / Chroma
- privacy admission
- Episode provenance / lineage
- Reflection
- Big Five Aspects Personality
- Relationship / Life State

これらをLetta Memoryへ移すと、今回守りたい固有領域までruntimeへ委譲することになる。
また2026-09時点の現行Agent SDKはPython版がなく、現在のFastAPI processへ直接埋め込む比較条件でも不利である。

したがってMVPでは採用せず、将来Memory正本そのものを再設計する場合のみ再評価する。

## 共通contractと関連Epicの境界

framework非依存の共通契約を維持し、DBOSの保存方式をpersona memory schemaへ持ち込まない。

- #249: runtime result、Life State、Autonomy Target、実行制御と関連serviceへの接続境界
- #100: Episode schema / SELF / experienced_at / source、Semantic / Reflectionの形成・永続化
- #101: Personality / Relationshipの更新
- #102: Procedural / Interpersonal Skill
- #185: High Impact / ActionRecovery / 通常writeの安全な実行

関連Epicが未実装の間は、接続先の不足をDEFERRED等の明示的な結果として扱う。
仮のReflection・SELF Episode・Personality更新を本実装の成功として返さない。
最初の自律活動シナリオは、許可されたELYTH MCPを通じた話題探索とする。

## production統合の境界

採用runtimeの依存は実装PRで固定versionとして追加する。起動・shutdown、system DB path、
backup対象、health checkを併せて定義する。LangGraph / Lettaは本実装の依存へ追加しない。

## 次の実装順

1. #100: Episode SQLite schema v3 / `SELF` / `experienced_at` / Reflection persistence
2. #249: DBOS bootstrap / Character Life queue / scheduler
3. #100: Episode bundle → Semantic / Reflection background pipeline
4. #249: Life State persistence / formation
5. #101: Personality / Relationship
6. read-only Autonomous Activity
7. #185完了後に通常writeを含むAutonomous Activity
8. #102 Procedural / Interpersonal Skill

## 参考

- DBOS Python programming guide: https://docs.dbos.dev/python/programming-guide
- DBOS queue: https://docs.dbos.dev/python/tutorials/queue-tutorial
- DBOS schedules: https://docs.dbos.dev/python/tutorials/scheduled-workflows
- LangGraph Functional API: https://docs.langchain.com/oss/python/langgraph/functional-api
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- Letta Agent SDK: https://docs.letta.com/agent-sdk

## 結果

**DBOSをCharacter Lifeのtop-level durable runtimeとして採用する。**

LangGraphは必要になった場合の認知フロー内部実装候補、LettaはMVP非採用とする。
