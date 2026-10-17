# ADR 0017: 記憶の形成と保存判定

Status: Accepted

一部を[ADR 0027](0027-tool-use-and-activity-runtime.md)のツール利用と実行基盤の決定で置き換えます（2026-10-11 ユーザー承認。§4 のうち、PrivateAgent が drain の起動・スケジュール・再試行・待機と再開を担う記述。PrivateAgent は使わず、timer が Core の drain API を呼ぶ。重複の防止と再試行は Core の job の台帳が持ち、中断した job は途中から再開せず、冪等性を保って次回に最初からやり直す。Core が永続予約・drain API・正本の検証を持つことと、キャラクターの判断に Core の LLM を使うことは維持）。

置換注記（2026-10-08）: [ADR 0022](0022-memory-retrieval-from-records.md)と[逐語記憶撤去の実施](../evidence/2026-10-08-verbatim-memory-removal.md)により、背景および後続境界で言及したMemoryService.extractと逐語の有限batch抽出は撤去済みです。構造化抽出・保存判定・形成jobは本ADRの後続実装として未実装のままです。

日付: 2026-10-06

Accepted日: 2026-10-07

根拠: 2026-10-06のユーザー決定（[SPEC §4.1の決定表](../../SPEC.md#41-決定済み2026-10-06のユーザー決定)）と
[PR #67](https://github.com/FYuki/digital-souls-core/pull/67)のレビュー。

## 背景

現行Coreの記憶形成は、trusted callerが明示したsource集合だけを有限batchで抽出する方式です
（[ADR 0007](0007-memory-provenance-and-revocation.md)）。PoCは保存済み会話を起点に非同期で
Episode・Fact・Semanticを形成し、保存判定を型付きの状態で行います。[ADR 0015](0015-memory-model-reorganization.md)
に従い、PoCの[RAG privacy方針](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/rag-memory-privacy-policy-2026-07.md)、
[Wave 2](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/wave2-memory-formation-retrieval-2026-08.md)第5〜9・12・16節、
[Episode・Fact・Semantic境界](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/episode-fact-semantic-boundaries-2026-09.md)第5〜7節を採用します。

## 決定

### 1. 保存可能な条件

次をすべて満たすCandidateだけを自動保存します。候補ごとの確認・保存通知・確認待ち状態は作りません。

```text
保存policyで許可された型
+ privacy検査を通過
+ 対象sourceが記憶の対象（ADR 0006の指定発話除外・プライベートモードでない）
+ 会話由来なら、元発言の履歴本文が保存済み
= 保存可能な検証済みCandidate
```

長期記憶の登録を有効にした状態で会話したことを、保存への同意として扱います（2026-10-06のユーザー決定、
[ADR 0019](0019-memory-correction-invalidation.md)）。
ユーザーの「覚えて」は候補抽出を促しますが、絶対禁止・意味分類・型の許可を迂回しません。
保存拒否の語を検出した発話は、確認が取れるまで形成の対象外として保留します（ADR 0019）。
保存後の内容は管理操作で閲覧・訂正・削除できるようにします（[ADR 0019](0019-memory-correction-invalidation.md)）。

### 2. 型付きの保存判定

| 状態 | 意味 | 副作用 |
| --- | --- | --- |
| DENY_SENSITIVE | 機微情報または絶対禁止 | なし |
| DENY_USER_REQUEST | ユーザーの保存拒否 | なし |
| ABSTAIN_UNKNOWN | 判定不能、分類器障害、timeout、不正出力 | なし |
| NOT_MEMORY_WORTHY | 安全だが型の許可外、または長期的価値なし | なし |
| ALLOW_STRUCTURED | 許可型へ正規化され、privacy検査を通過 | 検証済みCandidateとして保存可能 |

複数の判定がある場合は上の順で厳しい状態を優先します。ALLOW_STRUCTUREDだけが保存へ到達します。
LLMはBinding、保存先、許可型を選択しません。

### 3. 検査の責務分離

| 構成要素 | 責務 | 行わないこと |
| --- | --- | --- |
| 決定論的scanner | 秘密値・直接識別値の検出 | 文脈依存の意味分類、保存先の決定 |
| 意味分類器（ローカルのみ） | health、心理状態、第三者情報、暗示表現等の分類 | 型の許可変更、保存可否の決定 |
| 保存判定（admission evaluator） | scannerの結果、意味分類、候補型から最終状態を決定論的に算出 | LLM呼出し、DB・embedding操作 |

scannerが絶対禁止を確定した場合は分類器を呼ばずに拒否します。意味分類の前段にkeyword等の
意味的filterを置かず、許可型の候補はすべて分類器へ渡します。判定の由来は分類器・モデル・prompt・
policyのversionを独立して記録します。既存の[ADR 0005](0005-privacy-boundaries.md)のpermissionと
ローカル限定の分類器はこの構成の下層として維持します。

絶対保存禁止（認証情報、秘密鍵、決済・口座情報、政府ID、health・服薬・生体情報、住所・正確な位置、
私用連絡先、第三者の非公開情報）は、会話・人格prompt・LLM出力から緩和できません。

### 4. 会話からの非同期形成

会話応答は記憶形成の完了を待ちません。形成は次の流れとします。

```text
会話履歴の保存・スレッド更新
  -> 形成jobの永続予約（同じスレッドの未処理予約は集約、処理中の更新は再予約）
  -> 許可されたスレッドの範囲を取得（長文は境界の二重登録を防いで分割）
  -> Episode / Fact / Semantic候補の抽出（元発言・版・範囲と対応）
  -> schema・scanner・意味分類・sourceの検証
  -> 同一Binding・同一スレッドのFact照合（4節）
  -> 正本・参照・統合関係・派生index予定を同一transactionで保存
```

- 形成jobは永続化し、失敗・再起動後に未処理の版を回復できるようにします。
- 現在＋直前のturnだけに限定せず、スレッドの許可された範囲を対象にします。
  元発言ごとの除外・private・保存状態は、周囲の許可済み発言で洗い流しません。
- 保存直前にもsourceの版と有効性を再確認し、撤回済み・削除済みsourceから保存しません。
- 検索結果（RAGで取得した記憶）を新しい経験の根拠にしません。
- 同じ入力の再試行による二重保存の防止は、5Wによる同一性判定とは別に、元発言・版・範囲と
  保存結果の対応で行います。生本文のhashを冪等keyに使いません。
- 推論は会話を優先します。高負荷時は未処理を保持して形成を遅らせ、品質基準を下げません。

形成jobの実行は、PoCの
[Character Life §13](https://github.com/FYuki/digital-souls-poc/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/decisions/character-life-memory-personality-autonomy-2026-09.md)
に従い外部の実行基盤へ委譲します。ユーザーの決定（2026-10-06）により、実行基盤はPrivateAgentとし、
Coreに常駐workerを含めません。

- Coreは、形成jobの永続予約、有限のdrain処理（API）、正本の更新条件と検証を所有します。
- PrivateAgentは、drainの起動・スケジュール・再試行・待機と再開を担当します。
- キャラクターによる判断が必要な処理（抽出、一般化、内省、人格評価など）は、PrivateAgentが
  CoreのLLMをAPIとして呼び出します。Coreはそのpromptと人格文脈、保存前の検証を所有し、
  PrivateAgentが正本を直接書き換える経路は作りません。

### 5. 同一スレッドのFact照合と更新

照合範囲は、今回の候補同士と、同一Binding・同一スレッド由来の保存済みFactだけとします。
Factの5Wと文脈から同一の出来事への再言及と確認できたときだけ、版付きのID関係で同一Factとします。

- unknown同士、日/月の範囲包含、同名だけ、同日別回の可能性、否定と肯定、実施と予定は一致の根拠にしない。
- 統合を見送っても、保存条件を満たす情報は別Factとして保持し、破棄・日時補完しない。
- Fact統合でEpisodeを統合・削除しない。
- 同じスレッドの文脈と出典から、補足・訂正の対象Factを明確に特定できる場合は、Factの内容版を
  自動で更新する（「昨日うどんを食べた」「ごめん、そばだった」）。対象を特定できない場合は推測で上書きしない。

別スレッドのFact統合は後続の非同期整理とし、初期の形成の完了条件にしません。
別Bindingの照合・統合は全段階で拒否します。

### 6. Semanticの形成経路

- **DIRECT_EXTRACTION**：保存済み会話のuser発言を根拠に、明示された命題を抽出・検証する。
  assistant発言だけから確定しない。単一の観測から傾向を推測しない（一度紅茶を飲んだことから「紅茶好き」としない）。
- **EXPERIENCE_DERIVED**：保存済みの独立した2件以上のEpisodeから一般化する。件数だけで採用せず、
  不足・矛盾・独立性不明なら見送る。同じ話題の出来事を二重に数えない。

一般化とReflectionの形成は、Episode抽出・直接抽出とは別のtriggerで実行します（PoCでは夜間の固定時刻）。

### 7. 既存記憶の整理（consolidation）

同種の既存記憶の整理は、KEEP / MERGE / SUPERSEDE / DELETE_EXACT_DUPLICATE / CONFLICT / NOOPの
型付き計画で行い、applicationがBinding・版・privacy・出典・lineageを再検証して適用します。
曖昧な候補を自動削除せず、物理削除は完全一致の重複に限ります。会話・形成・index反映に未処理が
ある間は実行しません。Fact統合や一般化とは別の処理です。

## 影響

- 既存の明示抽出API（`MemoryService.extract`）は、管理・試験用の経路として残すか、形成jobの
  明示投入へ置き換えるかを実装時に決めます。
- 抽出器の出力schemaは型とsource indexから、構造化値と引用範囲へ拡張します。
  [ADR 0008](0008-managed-structured-output.md)の構造化出力とローカル検証を維持します。
- 評価はpromptfooとpytestを分け、固定ケース・期待値をprompt調整前に固定します（[SPEC](../../SPEC.md)）。

## 参照

- [ADR 0006](0006-conversation-memory-controls.md)：ユーザー指定の操作仕様（優先）
- [ADR 0016](0016-memory-kinds-and-records.md)：種別・5W・日時
