# 記憶・人格の仕様

Coreが満たす記憶・人格の機能要件、受入条件、実装状況をまとめます。
判断と理由は[ADR](docs/adr/README.md)、用語は[CONTEXT](CONTEXT.md)、作業単位と進捗は
[Issue](https://github.com/FYuki/digital-souls-core/issues)が正本です。この文書で仕様を新設せず、
ADRの決定を機能単位に整理します。

状態の意味は次のとおりです。

- **実装済み**：この文書と同じリビジョンに実装があり、合成データのUT/IT1・PostgreSQL合成試験で検証済み。実モデルでの受入は別。
- **一部**：基盤や一部の経路だけが実装済み。
- **未実装**：ADRで採用済みだが、この文書と同じリビジョンに実装がない。

実装状況は、この文書と同じリビジョンの実装を基準に照合します。
「撤去済み」は旧経路が削除され、現行機能として提供しないことを示します。

## 1. 前提

- 記憶モデルは、PoCで採用済みの記憶決定をCoreへ移設したものです（[ADR 0015](docs/adr/0015-memory-model-reorganization.md)）。
- ユーザーがCoreで指定した操作仕様（[ADR 0006](docs/adr/0006-conversation-memory-controls.md)）を優先します。
- 記憶・人格はBinding（subject・client・audience・character）で分離し、別Bindingの情報を混ぜません。
- privacyはfail-closedです。判定できない場合は保存・検索・送信をしません。ただし記憶の障害で会話は止めません。
- 履歴・記憶の保存先はPostgreSQLへ一本化します（[ADR 0021](docs/adr/0021-postgresql-only-storage.md)）。
  SQLiteは撤去済みです。ローカルでもDockerのPostgreSQLを使います。
- 1つのスレッドを長く使う利用を想定します。
- 長期記憶の登録を有効にした状態で会話したことを、保存への同意として扱います。製品として配布・販売する場合は、
  初回の画面でプライバシーポリシーの提示と同意の取得を行います。
- AIの判断では会話履歴を削除しません。削除はユーザーが削除機能で行います。

## 2. 機能と実装状況

### プロンプトの組み立て

| 機能 | 状態 | 根拠 |
| --- | --- | --- |
| 全入口共通のCard・Lore・記憶・呼び出し側指示・履歴・post_history・今回の入力の組み立て、領域token予算、往復/item単位の削減と本文を含まない400拒否 | 一部（ByteTokenCounterによる組み立て・予算・送信前認可は実装済み。推論先token実測・Runtime Contextの状態生成・実モデル受入は未実装/未実施） | [ADR 0026](docs/adr/0026-prompt-assembly.md)、[推論API](docs/api.md#文脈プライバシー) |
| 応答に使ったCardファイルの生バイト列SHA-256の起動時固定、全completionのheader、会話turnの保存・復元metadata | 実装済み（streamを含む合成試験。旧turnはNULL、再送fingerprint維持。人格評価ハーネス・実モデル評価は未実装/未実施） | [ADR 0029 §6](docs/adr/0029-persona-consistency-evaluation.md#6-card-のハッシュ)、[履歴API](docs/history-api.md) |

送信payloadの履歴削減は、保存済み会話の削除ではありません。固定人格・今回の入力・
直前の完了した1往復を保護し、tool callと結果の組を分けません。記憶の撤回guardは削減後も保持します。

### 2.1 会話履歴と記憶の操作

| 機能 | 状態 | 根拠 |
| --- | --- | --- |
| 会話履歴の明示的な保存・復元・一覧・削除 | 実装済み | [ADR 0004](docs/adr/0004-conversation-history.md) |
| 指定発話の記憶除外、スレッド単位のプライベートモード、アーカイブ | 実装済み | ADR 0006 |
| 履歴削除・private化による派生記憶の削除、残る出典からの再構成 | 一部（正本の撤回・本文消去は実装済み、再生成は未実装） | [ADR 0007](docs/adr/0007-memory-provenance-and-revocation.md)、[ADR 0022](docs/adr/0022-memory-retrieval-from-records.md) |
| 元発言の日時（stated_at）の保存 | 実装済み | [ADR 0016](docs/adr/0016-memory-kinds-and-records.md)、[履歴API](docs/history-api.md) |
| 保存拒否の語の検出、確認までの形成保留、プライベートモードと削除機能の案内 | 実装済み | [ADR 0019](docs/adr/0019-memory-correction-invalidation.md) |
| 会話往復単位の削除（削除範囲をユーザーが選択） | 実装済み | ADR 0019 |

保存拒否では、現在userの拒否語に対するJSON・SSE確認信号、永続的な形成保留、
revision排他付き回答APIを提供します。受入はprivate化・派生記憶の削除を
原子的に行い、解除しても旧記憶・受入発話を復活させません。拒否は該当保留だけを解除し、
明示除外を維持します。プライベートモード・会話全体の削除・往復単位の削除を案内し、
削除範囲はユーザーが選びます。履歴の自動削除は行いません。再起動・同一request再送・旧schema移行の
契約は[履歴API](docs/history-api.md#保存拒否の確認)に記載します。

往復単位の削除では、明示APIによる「選択した往復だけ」「選択した往復以降すべて」の削除を
PostgreSQLで提供します。tool対応がまたがる往復をまとめて物理削除し、
削除済みrequest IDの再送と旧revisionでの推論commitを拒否します。対象往復を出典に持つ記憶だけを
撤回し、本文と依存結果を即時に利用停止します。形成が実装されるまで再生成しない期間を許容します。
対象外の後続往復と新規往復は引き続き正本登録の出典にでき、自動形成は未実装です。
日時・確認状態と既存の会話全体削除・通知を維持します。詳細は
[往復単位の明示削除](docs/history-api.md#往復単位の明示削除)に記載します。

### 2.2 記憶の記録

| 機能 | 状態 | 根拠 |
| --- | --- | --- |
| 選択したuser発話を逐語で保存する旧記憶 | 撤去済み（schema版6） | ADR 0016、ADR 0022 |
| Episode（経験の5W、経験日時、一続きの経験の単位） | 一部（正本の型・schema・登録・撤回は実装済み、形成・経験単位の照合は未実装） | ADR 0016、ADR 0022 |
| Fact（独立ID、対象の5W・日時、Episode–Fact参照、内容版） | 一部（正本の登録・内容版更新・旧版保持・参照・撤回は実装済み、形成・照合は未実装） | ADR 0016、ADR 0022 |
| Semantic（DIRECT_EXTRACTION / EXPERIENCE_DERIVED、命題、適用時期） | 一部（両形成種別の正本登録・根拠検証・撤回は実装済み、直接抽出・一般化は未実装） | ADR 0016、ADR 0022 |
| 引用範囲と元発言の版の対応、保存文の構造化値からの生成 | 一部（引用のrevision・epoch・話者・文字範囲の検証は実装済み、保存文生成は未実装） | ADR 0016 |
| タイムゾーン設定と相対日時の解釈、部分日時・精度 | 一部（部分日時・精度・timezoneの保持と型付き範囲の保存は実装済み、設定・相対日時解釈は未実装） | ADR 0016 |
| 既存の逐語記憶の削除（移行しない） | 実装済み（schema版6で旧3表を削除） | ADR 0016、ADR 0022 |

正本のschema・登録・版・撤回の契約は合成試験で検証済みです。登録portはtrusted callerが
形成・保存判定を済ませた構造化記録を受け取り、抽出・保存価値の判断・保存文生成を代行しません。
Episode・SemanticのIDは版1で登録し、同一IDの内容版追加はFactだけが提供します。
詳細は[記憶API](docs/memory.md)を参照してください。

### 2.3 形成と保存判定

| 機能 | 状態 | 根拠 |
| --- | --- | --- |
| 決定論的scanner、ローカル意味分類器、permission | 実装済み | [ADR 0005](docs/adr/0005-privacy-boundaries.md) |
| 型付きの保存判定（DENY_* / ABSTAIN / NOT_MEMORY_WORTHY / ALLOW_STRUCTURED） | 未実装 | [ADR 0017](docs/adr/0017-memory-formation-admission.md) |
| 明示したsource集合からの有限batch逐語抽出 | 撤去済み | ADR 0007、ADR 0022 |
| 会話履歴の保存を起点とする形成jobの永続予約・集約・回復 | 未実装 | ADR 0017 |
| スレッド範囲の抽出、長文分割、冪等な登録 | 一部（正本の冪等登録は実装済み、抽出・長文分割は未実装） | ADR 0017 |
| 同一スレッドのFact照合、対象が明確な補足・訂正によるFact更新 | 一部（期待版を照合する内容版更新は実装済み、発話からのFact照合・訂正判断は未実装） | ADR 0017 |
| Semanticの直接抽出 | 未実装 | ADR 0017 |
| Episode群からの一般化（EXPERIENCE_DERIVED） | 未実装 | ADR 0017、[ADR 0020](docs/adr/0020-reflection-personality-relationship.md) |
| 既存記憶の整理（consolidation） | 未実装 | ADR 0017 |
| 別スレッドのFact統合 | 未実装（後続） | ADR 0017 |

### 2.4 検索と会話での利用

| 機能 | 状態 | 根拠 |
| --- | --- | --- |
| 部分文字列検索 | 撤去済み | ADR 0022 |
| 正本のEpisode・Semanticを候補とするembedding意味検索（Episodeに有効なFactを添付） | 実装済み | [ADR 0010](docs/adr/0010-in-process-memory-search.md)、[ADR 0011](docs/adr/0011-local-memory-embedding.md)、ADR 0022 |
| 検索前のquery判定（機微なqueryで検索しない） | 実装済み | [ADR 0018](docs/adr/0018-memory-retrieval-context.md) |
| embedding未接続・検索障害時に記憶なしで会話を継続 | 実装済み | ADR 0018、ADR 0022 |
| PoC互換の順位（候補20、閾値0.54、同等帯、最大5件） | 実装済み（永続した言及日時・作成日時・IDで同等帯を並べる） | ADR 0018、ADR 0022 |
| last_user_mentioned_atとTOUCH | 一部（日時の永続化・順位への利用は実装済み、再言及時のTOUCHは未実装） | ADR 0018、ADR 0022 |
| 期間検索（日時・季節）と一致種別の順位 | 未実装 | ADR 0018 |
| 語彙による補完、自己申告の現在値補完、矛盾の注意 | 未実装 | ADR 0018 |
| 有効期限・policy versionの互換による除外 | 未実装 | ADR 0018 |
| 永続的な派生index | 未実装 | ADR 0015 |
| 本番検索・contextによる意味検索・回答評価（Python / promptfoo） | ハーネス実装・実モデル各3回実施済み。検索・回答ともFAIL、品質未受入 | [ADR 0023](docs/adr/0023-semantic-evaluation-contract.md)、[実モデル証跡](docs/evidence/2026-10-09-semantic-real-model-evaluation-top5.md) |
| モデル向けcontextの一時参照名、送信直前の再検証 | 実装済み（保存文・部分日時・Factを渡し、逐語引用と保存IDは渡さない） | [ADR 0009](docs/adr/0009-memory-context-references.md)、ADR 0022 |

embedding未接続時はstorageを読まず空結果を返します。Factは独立のembedding候補にしません。
同等帯の順位は `last_user_mentioned_at DESC NULLS LAST → created_at DESC → id ASC` です。
TOUCH・検索・assistantの言及による日時更新は実装されていません。

### 2.5 訂正・削除・失効

| 機能 | 状態 | 根拠 |
| --- | --- | --- |
| 明示訂正と時間変化の区別、旧状態の履歴 | 一部（Factの旧内容版は保持、訂正と時間変化の判断は未実装） | [ADR 0019](docs/adr/0019-memory-correction-invalidation.md) |
| 固定属性の矛盾保留、自己申告と一般化の共存 | 未実装 | ADR 0019 |
| 管理操作による訂正（自己申告由来・Fact単位）と削除、再処理の防止 | 未実装 | ADR 0019 |
| 根拠の訂正・削除時の依存結果の即時利用停止と再評価 | 一部（出典撤回時の正本・依存結果の停止と本文消去、検索時の参照版検証は実装済み、再評価・再生成は未実装） | ADR 0019、ADR 0022 |

### 2.6 内省・人格・関係

| 機能 | 状態 | 根拠 |
| --- | --- | --- |
| Reflection（ACTIVE / SUPERSEDED / INACTIVE）と派生（Insight・Interest・Intention） | 未実装 | ADR 0020 |
| Life State | 未実装 | ADR 0020 |
| Big Five Aspects 10因子の人格と上限付き更新 | 未実装 | ADR 0020 |
| Relationship State（2軸）とInterpersonal Skill | 未実装 | ADR 0020 |
| Procedural Memory / Skillの学習 | 未実装 | [CONTEXT](CONTEXT.md) |

## 3. 受入条件

### 3.1 観測可能な振る舞い

各機能の受入では、少なくとも次を確認します。詳細なシナリオはPoCの要件書
（[#340](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/epic-340-episodic-memory-requirements.md)、
[#341](https://github.com/FYuki/digital-souls/blob/fce7382884d981c42be7fbd3ddaffe7469e27588/docs/epic-341-semantic-memory-requirements.md)）を
Coreの実装Issueへ移して管理します。

- 述語だけのEpisodeを保存でき、不明な5W・日時を補完しない。
- 「話を聞いた」経験で、キャラクターを話題の出来事の参加者にしない。仮定・創作を実体験に読み替えない。
- 5Wと文脈が一致する同一スレッドの再言及だけを統合し、unknown・日/月の包含・同名・別回・否定と予定を統合しない。
- 「ごめん、そばだった」のような明確な訂正でFactが更新され、以後の会話で訂正後の内容を使う。
- 単一の発言から知識を取得し、別のスレッドで想起できる。出来事や推測を知識に読み替えない。
  キャラクター自身の発言だけを根拠にしない。
- 明示訂正と時間変化を区別し、固定属性の食い違いを断定しない。一般化と自己申告が食い違うと自己申告を優先する。
- 削除・private化・出典撤回の後、古いindex・統合履歴・再試行から削除済みの内容が復活しない。
- 機微なqueryでは検索しない。検索障害では記憶なしで会話を続ける。
- 同じ関連度の帯では最近言及された記憶を優先し、明確に関連度の低い記憶を繰り上げない。
- 別Bindingの記憶が検索・形成・統合のどの段階にも混入しない。
- 会話応答が記憶形成の完了を待たない。

### 3.2 評価の基準

- 評価ケースと期待値は、prompt調整の前にcommitで固定する。結果に合わせて期待値や閾値を緩めない。
- 記憶判断のモデル評価は、PoCと同じく**cacheなしで3回実行し、各回・各分類で90%以上**、
  **禁止情報の保存と別キャラクターの混入は0件**を合格とする。
- 機微なケースがNOT_SENSITIVEになることを許容しない。SENSITIVE・ABSTAINは安全側として許容する。
- privacy・Binding・出典・失効の必須ゲートは、平均点や他のケースで相殺しない。
- enum等は決定論的に採点し、自由文の評価に独立したjudgeを使う場合は採点基準とjudgeを固定する。
- mock・fixtureの成功を実モデル品質の受入とみなさない。合成正本の直接登録と実モデルによる検索・回答評価は、
  [ADR 0023](docs/adr/0023-semantic-evaluation-contract.md)の合成セット品質として区別し、形成〜利用の実接続受入とみなさない。
- 意味検索の評価は、本番と同じ検索設定（最大5件、relevance閾値0.54、同等帯）で行う。`relevant_ids` の全包含を品質条件として維持し、
  該当なしのケースでは閾値を超える候補が1件もないことを求める。
- 検索の必須ゲートはPoCのRAG評価と同じく、privacy・Binding境界の違反0件、閾値未満の混入0件、
  検証できない記憶への代替0件を維持する。実モデルでは、1位にあるべき記憶が返却上位5件に含まれることを求め、
  順序一致は求めない。1位はgoldの `expected_order` の先頭、なければ `relevant_ids` の先頭、空なら該当なしとする。
  同等帯の全順序一致はfixtureの62ケース×3回で本番の並べ替えを検証する必須ゲートとして維持する。
  モデルに依存する品質は上記の3回・90%で判定する。
- 検索はPython、回答はpromptfooで本番検索・contextを通す。回答は必須事実グループの全包含と禁止事実の不在を
  NFKC→casefoldの部分文字列一致で判定し、LLM judge・出典表記の採点は行わない（ADR 0023）。
  dispatch有効性はgoldと一致させ、valid=trueでは送信上位5件への1位の記憶の包含を要求する。
  top-1なしならID照合なし、valid=falseならID空を要求し、no_memoryの空context・lifecycleは維持する。
  品質は上記の3回・各分類90%で判定し、privacy・Binding・失効の必須ゲートは相殺しない。
- 回答の引用形式の揺れは、製品側で回答を読み取る際に正規化するかを別途決める。
- 実接続の受入は、dev環境の専用データと合成シナリオで、通常の会話からの形成・保存・検索・応答での
  利用までを確認する。訂正後の内容を使い、削除・無効化した内容を使わないことも確認する。
  実行commit、モデル・設定、シナリオ、期待値、結果、未検証事項を記録する。

正本へ合成登録したnomic / gemma4-12bの検索・回答評価は、上位5件包含の基準でcacheなし各3回再実施済み、いずれもFAIL。
検索は日英・無関係・閾値分類の品質未達とtop-1欠落・該当なしゲート違反、
回答は日英分類の品質未達とdispatch・空contextゲート違反があり、品質は未受入です。
各回・分類・ケースの結果と制約は[実モデル証跡](docs/evidence/2026-10-09-semantic-real-model-evaluation-top5.md)にあります。
分類器品質・形成〜利用の実環境IT2/STは未実施です。

## 4. 決定事項と要決定事項

### 4.1 決定済み（2026-10-06のユーザー決定）

| 論点 | 決定 | 反映先 |
| --- | --- | --- |
| 形成jobを実行する主体 | PrivateAgentが実行する。Coreは永続予約・有限のdrain API・正本の検証を提供し、常駐workerを持たない。キャラクターによる判断が必要な処理はCoreのLLMをAPIとして呼ぶ。（2026-10-11 のユーザー決定で置き換え：PrivateAgentは使わず、timerがCoreのdrain APIを呼ぶ。重複の防止と再試行はCoreのjobの台帳が持ち、中断したjobは次回に最初からやり直す） | ADR 0017、ADR 0020、[ADR 0027](docs/adr/0027-tool-use-and-activity-runtime.md) |
| 自然文による保存拒否 | 語を検出し、スレッドをプライベートモードへ切り替えるかをユーザーに確認する。切替は既存の明示APIで行う | ADR 0019 |
| 会話外の活動でCoreが所有する契約 | 機微情報の流出防止ゲート（送信直前の再判定）と、インジェクション対策 | ADR 0020 |
| 既存の逐語記憶 | 新形式へ移行せず削除する。dogfoodはCoreへの切替中で運用前 | ADR 0016 |
| 保存への同意 | 長期記憶の登録を有効にした状態での会話を同意とする。配布・販売時は初回画面でポリシー提示と同意取得 | ADR 0019 |
| 保存拒否の語を検出した発話 | 確認が取れるまで記憶形成の対象外として保留する。切替を断った場合だけ対象へ戻す | ADR 0019 |
| AIによる履歴削除 | 行わない。プライベートモードと削除機能を案内するだけ | ADR 0019 |
| 会話往復単位の削除 | 削除機能で削除範囲をユーザーが選べるようにし、往復単位の削除を追加する | ADR 0019 |
| 意味検索の評価基準 | 本番と同じ検索設定で評価し、PoCのRAG評価の必須ゲートと3回・90%を適用する（3.2） | ADR 0018 |

### 4.2 要決定事項

現在、要決定事項はありません。新たな論点はIssueで扱い、決定後にこの文書とADRへ反映します。

## 5. 実装の順序（案）

依存の少ない順に、Epic・Issueへ分解します。正本の保存と検索・逐語記憶の撤去は完了し、
構造化形成へ進む段階です。以下は完了した基盤と残る作業を分けた順序です。

1. 既存の逐語記憶の削除・履歴へのstated_at追加は実装済み。タイムゾーン設定・相対日時解釈は未実装
2. 検索障害時の会話継続・PoC互換の順位・保存拒否の語の検出と確認の信号・会話往復単位の削除は実装済み
3. Episode・Fact・SemanticのPostgreSQL正本schema・登録・版・撤回・契約試験と正本検索は実装済み（データ移送は行わない）。
   本番検索・contextの評価ハーネスと実モデル各3回の測定は実施済み、検索・回答品質はFAILで未受入
4. 型付きの保存判定と、構造化Candidateの抽出・検証
5. 形成jobの永続予約と非同期形成、同一スレッドのFact照合・更新
6. TOUCH、期間検索、語彙・自己申告の補完、矛盾の注意（last_user_mentioned_atの保持・順位利用は実装済み）
7. 訂正・時間変化・矛盾・管理操作・依存結果の失効
8. 一般化とReflection、Life State、人格・関係の更新
9. consolidation、別スレッドのFact統合、永続的な派生index
10. 会話外の活動のAgentがCoreのMCP経由で使う、機微情報の流出防止ゲートとインジェクション対策の境界（ADR 0027）
