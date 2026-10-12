# 形成評価の固定合成ケース

[Issue #206](https://github.com/FYuki/digital-souls-core/issues/206) のデータ移行・形式検証です。
評価契約は [ADR 0033](../../docs/adr/0033-memory-formation-evaluation.md)（Proposed）。
採点・ハーネスは #212、実モデル実行は #213。形式 UT は品質受入ではありません。

## 形式と固定

各 dataset の cases.json は schema_version・dataset・id と turn 列（role・text・任意 stated_at）だけです。
category・対象行為・gold・既存記録は推論入力へ含めません。本文は文字列として元と完全一致です。
PoC の対の turn は user→assistant のメッセージ列へ展開し、空の話者スロットは追加しません。
期待値は expectations.json、生成 schema は上位の cases.schema.json・expectations.schema.json です。
[loader](../../src/digital_souls_core/formation_evaluation_cases.py) は strict・extra=forbid で
集合・id・列挙値・日時精度・引用の話者/turn/文字範囲・期待値の矛盾を検査します。エラーに本文を出しません。
仕様変更ではなく評価専用 schema 1 の仮置きです。製品の公開 API・既定値は定義しません。

records は期待する記録への部分制約であり、全正本を捏造する snapshot ではありません。
未指定フィールドは「制約なし」、mode=null は「不明で補完しない」を区別します。
count は指定された種別の件数。type_and_topic の none だけは全候補0件です。
PoC が先頭候補の種別だけを測る type_and_topic は count=null を保ち、件数1へ読み替えません。
ただし Semantic への対応ではユーザー根拠と DIRECT_EXTRACTION を要求します。
引用は [start,end) の文字位置（bytes でない）。citations は選択可能な範囲であり、全文引用を強制しません。
元と一致する非空の実引用・source revision/epoch/Binding を後続で検証します。
why.text には明示理由の Citation、linked には全 Fact の正しい Episode 関連と共通 topic を要求します。
PoC が判定対象を事前指定する ground/actor は、その対象を gold の selector として固定し、モデルへは渡しません。
候補が見つからない結果は正解にせず、前段の抽出と行為者判断の成績を後続で区別する案です（要確認）。

## 出典と監査

repository: 公開 FYuki/digital-souls（https://github.com/FYuki/digital-souls）。
commit: `fce7382884d981c42be7fbd3ddaffe7469e27588`（fce7382）。以下は元ファイル bytes の SHA-256。
ファイルのない README/score/thresholds/gate/conformance は推測作成しません。
source/*.jsonl は合成原本 bytes をそのまま保持した監査・保留資料であり、評価入力ではありません。
gold を含むため推論へ渡すことは禁止します。active loader は cases/expectations のみを読みます。
migration.json は全 id の移植/保留の分割、元 hash、入力+gold の版 hash を固定します。
元 gold の canonical JSON hash も各 expectation に保持し、原本と照合します。
PoC の実ユーザーログや実行結果は転記していません。

| 元 path | 元 bytes SHA-256 |
| --- | --- |
| `backend/evals/memory_extraction/cases.jsonl` | `36fea6dde28493aee6827d3224508a6f833b0d08583321d21490b4c5c5287d13` |
| `backend/evals/memory_extraction/thresholds.json` | `4c29a1252699eaf4c85b069456f5e8786eeb6fad97c3d266a283163e36e2370e` |
| `backend/evals/memory_extraction/gate.py` | `fd8a95bb2758326bd2137c6295c9a109b2a6aecef40bdd23ce9b291f2d29d535` |
| `backend/evals/memory_extraction/conformance.yaml` | `d10af773f0846a86db82ae8d341d7ee08555393568a6ea9b4be03b62125ce803` |
| `backend/evals/memory_extraction/provider.py` | `7e2aaff2fd98cdda56bcf9e5843695a92adea761f1e4a2eec04c56ba026868c1` |
| `backend/evals/episodic_quality/cases.jsonl` | `a7d37f67abd205ec558658c554204a73cd8a636a443485f3c632ad8da76cf1d2` |
| `backend/evals/episodic_quality/README.md` | `957b13797c2c2c6da2f94627a3823aaf4d4eaa86b443f98e910ceb2ebbc0ee8a` |
| `backend/evals/episodic_quality/thresholds.json` | `d75926f649b810c94f4d7c9cf3c360e73a2978697f6fc6c5fd41c9544beffe7f` |
| `backend/evals/episodic_quality/gate.py` | `24bf2dfecd47aecc971f7c24a45e18a06f452296c20aea5305e2db6d8cdaf29a` |
| `backend/evals/episodic_quality/score.py` | `6c68c9bd3f6a3a02d18861b6731a9f0159447b309da14fba476bc8455e1f3e9c` |
| `backend/evals/episodic_quality/conformance.yaml` | `cd66936d1ab740412ad794d53106ca00e96fd0bf6e6bf4bcb45cdf12b240f741` |
| `backend/evals/episodic_quality/provider.py` | `c6f190a4473cedfb7542a63bf0c296c987bdef41fff1e9dec856758f5a2ec665` |
| `backend/evals/episodic_actor/cases.jsonl` | `130967b63c373080180f7e2c46fe3c972db6006c1b1795c3da3837dcdf105289` |
| `backend/evals/episodic_actor/README.md` | `ded836d9386f8c31cf6137c253cb55cd9fa5b6fb536535f515b1e9d2950199f6` |
| `backend/evals/episodic_actor/thresholds.json` | `80279079cd74324f504486450880095be95cbcfc371385b8373ca9b67b966b35` |
| `backend/evals/episodic_actor/gate.py` | `286f8f9fa91d7c6aeff70cd4456a91cfaa16bb7fcb3823924c72723527656af6` |
| `backend/evals/episodic_actor/score.py` | `1bc62dc790ae4f0f1daa86e63b12f528f4e09716419b3d583e76b7b07cc08ab5` |
| `backend/evals/episodic_actor/conformance.yaml` | `e0bb8e76f36cb9605a830ab23fd2ec72b2545818bafabaf48102b97cdfb90e5e` |
| `backend/evals/episodic_actor/provider.py` | `5be1b5ad67803b4bf8af3ec131bd8ea7a495c2ccb0c07e220dd7bf95bb0fa1ad` |
| `backend/evals/semantic_memory/cases.jsonl` | `1603788df455b6598ab328468c9fe048692efce51c851b87ab0749efc6fdd676` |
| `backend/evals/semantic_memory/README.md` | `5914d7d7079ac1659a0d060bd4af6a058401edee83b47867d8ce0c7fcf59ef53` |
| `backend/evals/semantic_memory/score.py` | `db9f772d23818ab28b98b80009ef85bed8859c8e771edbdbb2ae14d03c1f112e` |
| `backend/evals/semantic_memory/provider.py` | `1f9c9449f0881209b03073fd1eed5be1aa72a88bded38c9ab1dc62500c0f6fc3` |
| `backend/evals/semantic_memory/boundary-cases.jsonl` | `d2546ace86fcdf6f86d4309dc393cfdc47b6bba5c0cdaddd49a2e0a80cdd1df7` |
| `docs/epic-340-episodic-memory-requirements.md` | `c3bb9e83561565d9b8806581a6d1d8d2ae3bba003d167a4168217857433a89ab` |
| `docs/epic-341-semantic-memory-requirements.md` | `c36e6e93b22e5dc4042949e6b4fcae1a1120dea4b9da413e364ed65677868586` |

## 対応規則

| PoC | Core の評価語彙 | 維持する条件・制約 |
| --- | --- | --- |
| EPISODIC_EVENT | Episode の normalized_text/topic | キャラクターは話を聞いた経験の所有者。話題の参加者にしない |
| USER_PREFERENCE / INTERACTION_PREFERENCE | Semantic・DIRECT_EXTRACTION | 好み/対話希望を明示命題として扱う対応案。元 topic の rubric を固定。承認待ち |
| NONE | 全候補0件 | none_fact は一般知識との対応が未確定なので保留 |
| kind=FACT / count / NEW | Fact の件数・新規登録 | 指定がない件数を新設しない。fields は全て AND |
| what.object / where.name / why | five_w.object / where / why.text | contains_any と null を保持。理由は元引用を必須 |
| when.parts の既知年月日 | target_time の PartialDateTime | year/month/day 精度。null成分を埋めない。unknown は空 TemporalValue |
| FICTIONAL | 対応する Episode.context=fiction と Fact の文脈保持 | Fact を現実に読み替えない。CONDITIONAL/NEGATED/PLANNED は別 enum がないため保留 |
| ACTOR 集合 | Fact の Who 制約（who） | 元 entity_id と names の許容集合・全員一致・余分/重複禁止。Core の who テキストへの投影は要確認 |
| unknown_actor の空 ACTOR | Who が不明 | キャラクターのIDから補完しない |
| count=2, linked | Fact 2件 + EpisodeFactLink | 全 Fact の関連、対象語と Episode topic の対応を保持 |
| anchor・sources・evidence_tokens | Citation の話者・turn・範囲・引用語 | source の版・Binding・引用一致も必須。primary_from 付きは保留 |
| Semantic NEW/value_contains/self_report | Semantic・DIRECT_EXTRACTION・proposition.value/subject | 元 contains とユーザー自己申告の根拠を保持 |
| source_turn=n | required_user_turn=2*n | 対の user/assistant を展開後の user メッセージ位置。確認発言を出典にする |
| count=0 / forbidden_save | Semantic 0件 / 保存禁止ゲート | event_exclusion は Episode/Fact 全体の保存禁止にしない。boundary も Semantic 形成を禁止 |

## 全件照合と保留

| dataset | 元件数 | 移した件数 | 保留（移さなかった） |
| --- | ---: | ---: | ---: |
| memory_extraction | 30 | 29 | 1 |
| episodic_quality | 170 | 42 | 128 |
| episodic_actor | 100 | 100 | 0 |
| semantic_memory | 100 | 45 | 55 |
| semantic_boundary | 4 | 1 | 3 |

保留は除外を承認したという意味ではありません。**要確認（ユーザー判断）**:
A: 将来評価用に原本+id+goldを保留固定する（推奨。情報を失わず範囲を区別できる）。
B: 今回の評価からのみ外し、原本への参照だけ残す。C: fixture を許可する後続契約で現在のセットへ追加する。
現状は A の仮置きとして原本を保持し、保留件数を PASS の分母へ混ぜません。
Fact照合/訂正と別 Binding gate は必須だが、該当 fixture が未移植なので評価網羅を主張しません。
relative time、hypothetical/negation/planned、primary 範囲も保留のまま合格にしません。

### memory_extraction

移した id（29件）: `episodic_achievement`, `episodic_decision`, `episodic_outcome`, `episodic_change`, `episodic_shared`, `preference_like_tea`, `preference_like_music`, `preference_dislike`, `preference_over`, `preference_place`, `interaction_tone`, `interaction_length`, `interaction_language`, `interaction_format`, `interaction_address`, `none_question`, `none_greeting`, `none_command`, `none_assistant`, `episodic_exam`, `episodic_move`, `episodic_job`, `preference_food`, `preference_color`, `preference_dislike_noise`, `interaction_detail`, `interaction_casual`, `none_ack`, `none_lookup`。

保留 id と理由:

- `none_fact`: 一般知識の確認を一律 NONE とする旧抽出器の期待値は Core の Semantic 範囲と対応が未確定。期待値を変更せず要確認。

### episodic_quality

移した id（42件）: `selection-01`, `selection-02`, `selection-03`, `selection-04`, `selection-05`, `selection-06`, `selection-07`, `selection-08`, `selection-09`, `selection-10`, `what_where_why-01`, `what_where_why-02`, `what_where_why-03`, `what_where_why-04`, `what_where_why-05`, `what_where_why-06`, `what_where_why-07`, `what_where_why-08`, `what_where_why-09`, `what_where_why-10`, `temporal-06`, `temporal-07`, `temporal-08`, `temporal-10`, `context-04`, `context-05`, `context-06`, `links-01`, `links-02`, `links-03`, `links-04`, `links-05`, `links-06`, `links-07`, `links-08`, `links-09`, `links-10`, `evidence-01`, `evidence-02`, `evidence-03`, `evidence-04`, `evidence-05`。

保留 id と理由:

- `episode_boundary-01`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `episode_boundary-02`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `episode_boundary-03`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `episode_boundary-04`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `episode_boundary-05`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `episode_boundary-06`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `episode_boundary-07`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `episode_boundary-08`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `episode_boundary-09`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `episode_boundary-10`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `fact_operation-01`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `fact_operation-02`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `fact_operation-03`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `fact_operation-04`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `fact_operation-05`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `fact_operation-06`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `fact_operation-07`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `fact_operation-08`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `fact_operation-09`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `fact_operation-10`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `temporal-01`: 相対日時の発言基準時刻が元入力にない、または年不明の月。Core PartialDateTime に推測補完なしで写せない。
- `temporal-02`: 相対日時の発言基準時刻が元入力にない、または年不明の月。Core PartialDateTime に推測補完なしで写せない。
- `temporal-03`: 相対日時の発言基準時刻が元入力にない、または年不明の月。Core PartialDateTime に推測補完なしで写せない。
- `temporal-04`: 相対日時の発言基準時刻が元入力にない、または年不明の月。Core PartialDateTime に推測補完なしで写せない。
- `temporal-05`: 相対日時の発言基準時刻が元入力にない、または年不明の月。Core PartialDateTime に推測補完なしで写せない。
- `temporal-09`: 相対日時の発言基準時刻が元入力にない、または年不明の月。Core PartialDateTime に推測補完なしで写せない。
- `context-01`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-02`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-03`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-07`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-08`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-09`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-10`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `evidence-06`: primary/context_before の抽出所有範囲が必須。turn 列だけでは所有範囲を保持できず、引用検査だけへ緩めない。
- `evidence-07`: primary/context_before の抽出所有範囲が必須。turn 列だけでは所有範囲を保持できず、引用検査だけへ緩めない。
- `evidence-08`: primary/context_before の抽出所有範囲が必須。turn 列だけでは所有範囲を保持できず、引用検査だけへ緩めない。
- `evidence-09`: primary/context_before の抽出所有範囲が必須。turn 列だけでは所有範囲を保持できず、引用検査だけへ緩めない。
- `evidence-10`: primary/context_before の抽出所有範囲が必須。turn 列だけでは所有範囲を保持できず、引用検査だけへ緩めない。
- `catalog-01`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `catalog-02`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `catalog-03`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `catalog-04`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `catalog-05`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `catalog-06`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `catalog-07`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `catalog-08`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `catalog-09`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `catalog-10`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `same_event-01`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `same_event-02`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `same_event-03`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `same_event-04`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `same_event-05`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `same_event-06`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `same_event-07`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `same_event-08`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `same_event-09`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `same_event-10`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `privacy-semantic-01-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-01-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-02-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-02-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-03-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-03-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-04-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-04-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-05-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-05-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-06-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-06-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-07-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-07-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-08-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-08-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-09-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-09-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-10-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-10-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-11-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-11-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-12-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-12-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-13-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-13-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-14-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-14-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-15-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-15-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-16-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-16-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-17-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-17-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-18-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-18-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-19-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-19-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-20-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-20-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-21-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-21-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-22-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-22-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-23-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-23-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-24-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-24-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-25-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-25-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-26-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-26-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-27-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-27-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-28-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-28-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-29-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-29-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-30-ja`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `privacy-semantic-30-en`: 保存前分類器単体の classification gold。NOT_SENSITIVE を保存許可へ読み替えられず、ABSTAIN の扱いは ADR 0032 と異なる。既存 privacy 評価との分担を要確認。
- `merge_proposal-01`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `merge_proposal-02`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `merge_proposal-03`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `merge_proposal-04`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `merge_proposal-05`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `merge_proposal-06`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `merge_proposal-07`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `merge_proposal-08`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `merge_proposal-09`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。
- `merge_proposal-10`: 既存記録・catalog fixture と対象 ID/版/操作が必須。turn 列だけでは前提を保持できない。Core の照合/訂正評価への fixture 移行を保留。

### episodic_actor

移した id（100件）: `assistant_retelling-01`, `assistant_retelling-02`, `assistant_retelling-03`, `assistant_retelling-04`, `assistant_retelling-05`, `assistant_retelling-06`, `assistant_retelling-07`, `assistant_retelling-08`, `assistant_retelling-09`, `assistant_retelling-10`, `assistant_self-01`, `assistant_self-02`, `assistant_self-03`, `assistant_self-04`, `assistant_self-05`, `assistant_self-06`, `assistant_self-07`, `assistant_self-08`, `assistant_self-09`, `assistant_self-10`, `multiple_actors-01`, `multiple_actors-02`, `multiple_actors-03`, `multiple_actors-04`, `multiple_actors-05`, `multiple_actors-06`, `multiple_actors-07`, `multiple_actors-08`, `multiple_actors-09`, `multiple_actors-10`, `named_third_person-01`, `named_third_person-02`, `named_third_person-03`, `named_third_person-04`, `named_third_person-05`, `named_third_person-06`, `named_third_person-07`, `named_third_person-08`, `named_third_person-09`, `named_third_person-10`, `quoted_first_person-01`, `quoted_first_person-02`, `quoted_first_person-03`, `quoted_first_person-04`, `quoted_first_person-05`, `quoted_first_person-06`, `quoted_first_person-07`, `quoted_first_person-08`, `quoted_first_person-09`, `quoted_first_person-10`, `quoted_second_person-01`, `quoted_second_person-02`, `quoted_second_person-03`, `quoted_second_person-04`, `quoted_second_person-05`, `quoted_second_person-06`, `quoted_second_person-07`, `quoted_second_person-08`, `quoted_second_person-09`, `quoted_second_person-10`, `third_person_reference-01`, `third_person_reference-02`, `third_person_reference-03`, `third_person_reference-04`, `third_person_reference-05`, `third_person_reference-06`, `third_person_reference-07`, `third_person_reference-08`, `third_person_reference-09`, `third_person_reference-10`, `unknown_actor-01`, `unknown_actor-02`, `unknown_actor-03`, `unknown_actor-04`, `unknown_actor-05`, `unknown_actor-06`, `unknown_actor-07`, `unknown_actor-08`, `unknown_actor-09`, `unknown_actor-10`, `user_context-01`, `user_context-02`, `user_context-03`, `user_context-04`, `user_context-05`, `user_context-06`, `user_context-07`, `user_context-08`, `user_context-09`, `user_context-10`, `user_first_person-01`, `user_first_person-02`, `user_first_person-03`, `user_first_person-04`, `user_first_person-05`, `user_first_person-06`, `user_first_person-07`, `user_first_person-08`, `user_first_person-09`, `user_first_person-10`。

保留 id と理由:

なし。

### semantic_memory

移した id（45件）: `direct-01`, `direct-02`, `direct-03`, `direct-04`, `direct-05`, `direct-06`, `direct-07`, `direct-08`, `direct-09`, `direct-10`, `event_exclusion-01`, `event_exclusion-02`, `event_exclusion-03`, `event_exclusion-04`, `event_exclusion-05`, `event_exclusion-06`, `event_exclusion-07`, `event_exclusion-08`, `event_exclusion-09`, `event_exclusion-10`, `confirmation-01`, `confirmation-02`, `confirmation-03`, `confirmation-04`, `confirmation-05`, `confirmation-06`, `confirmation-07`, `confirmation-08`, `confirmation-09`, `confirmation-10`, `grounding_exclusion-01`, `grounding_exclusion-02`, `grounding_exclusion-03`, `grounding_exclusion-04`, `grounding_exclusion-05`, `grounding_exclusion-06`, `grounding_exclusion-07`, `grounding_exclusion-08`, `grounding_exclusion-09`, `grounding_exclusion-10`, `safety-01`, `safety-02`, `safety-03`, `safety-04`, `safety-05`。

保留 id と理由:

- `reaffirm-01`: 既存 Semantic の再言及/REAFFIRMには既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `reaffirm-02`: 既存 Semantic の再言及/REAFFIRMには既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `reaffirm-03`: 既存 Semantic の再言及/REAFFIRMには既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `reaffirm-04`: 既存 Semantic の再言及/REAFFIRMには既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `reaffirm-05`: 既存 Semantic の再言及/REAFFIRMには既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `reaffirm-06`: 既存 Semantic の再言及/REAFFIRMには既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `reaffirm-07`: 既存 Semantic の再言及/REAFFIRMには既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `reaffirm-08`: 既存 Semantic の再言及/REAFFIRMには既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `reaffirm-09`: 既存 Semantic の再言及/REAFFIRMには既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `reaffirm-10`: 既存 Semantic の再言及/REAFFIRMには既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `change-01`: ADR 0019 の時間変化には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `change-02`: ADR 0019 の時間変化には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `change-03`: ADR 0019 の時間変化には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `change-04`: ADR 0019 の時間変化には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `change-05`: ADR 0019 の時間変化には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `change-06`: ADR 0019 の時間変化には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `change-07`: ADR 0019 の時間変化には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `change-08`: ADR 0019 の時間変化には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `change-09`: ADR 0019 の時間変化には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `change-10`: ADR 0019 の時間変化には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `correction-01`: ADR 0019 の Semantic 訂正には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `fixed_conflict-01`: ADR 0019 の固定属性矛盾には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `correction-02`: ADR 0019 の Semantic 訂正には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `fixed_conflict-02`: ADR 0019 の固定属性矛盾には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `correction-03`: ADR 0019 の Semantic 訂正には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `fixed_conflict-03`: ADR 0019 の固定属性矛盾には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `correction-04`: ADR 0019 の Semantic 訂正には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `fixed_conflict-04`: ADR 0019 の固定属性矛盾には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `correction-05`: ADR 0019 の Semantic 訂正には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `fixed_conflict-05`: ADR 0019 の固定属性矛盾には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `correction-06`: ADR 0019 の Semantic 訂正には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `fixed_conflict-06`: ADR 0019 の固定属性矛盾には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `correction-07`: ADR 0019 の Semantic 訂正には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `fixed_conflict-07`: ADR 0019 の固定属性矛盾には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `correction-08`: ADR 0019 の Semantic 訂正には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `fixed_conflict-08`: ADR 0019 の固定属性矛盾には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `correction-09`: ADR 0019 の Semantic 訂正には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `fixed_conflict-09`: ADR 0019 の固定属性矛盾には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `correction-10`: ADR 0019 の Semantic 訂正には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `fixed_conflict-10`: ADR 0019 の固定属性矛盾には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `self_report_priority-01`: EXPERIENCE_DERIVED との共存/自己申告優先には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `self_report_priority-02`: EXPERIENCE_DERIVED との共存/自己申告優先には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `self_report_priority-03`: EXPERIENCE_DERIVED との共存/自己申告優先には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `self_report_priority-04`: EXPERIENCE_DERIVED との共存/自己申告優先には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `self_report_priority-05`: EXPERIENCE_DERIVED との共存/自己申告優先には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `self_report_priority-06`: EXPERIENCE_DERIVED との共存/自己申告優先には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `self_report_priority-07`: EXPERIENCE_DERIVED との共存/自己申告優先には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `self_report_priority-08`: EXPERIENCE_DERIVED との共存/自己申告優先には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `self_report_priority-09`: EXPERIENCE_DERIVED との共存/自己申告優先には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `self_report_priority-10`: EXPERIENCE_DERIVED との共存/自己申告優先には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `safety-06`: 別 Binding の既存記録の隔離には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `safety-07`: 別 Binding の既存記録の隔離には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `safety-08`: 別 Binding の既存記録の隔離には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `safety-09`: 別 Binding の既存記録の隔離には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `safety-10`: 別 Binding の既存記録の隔離には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。

### semantic_boundary

移した id（1件）: `boundary-03`。

保留 id と理由:

- `boundary-01`: 既存の否定的好みと出来事の境界には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `boundary-02`: 既存の否定的好みと出来事の境界には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
- `boundary-04`: 既存の否定的好みと出来事の境界には既存知識 fixture・旧状態の検査が必須。turn だけへ縮小せず保留。
