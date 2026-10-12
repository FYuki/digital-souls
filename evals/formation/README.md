# 形成評価の固定合成ケース

[Issue #206](https://github.com/FYuki/digital-souls-core/issues/206) のデータ移行・形式検証です。
評価契約は [ADR 0033](../../docs/adr/0033-memory-formation-evaluation.md)（Proposed）。
採点・ハーネスは #212、実モデル実行は #213。形式 UT は品質受入ではありません。

## 形式と固定

各 dataset の cases.json は schema_version・dataset・id、turn 列（role・text・stated_at）、
Binding・conversation_id・seed・clock_source・timezone を持ちます。seed は形成前の既存正本の状態です。
category・対象行為・期待操作/期待結果・gold は expectations 側に分離します。本文は元と完全一致です。
PoC の対の turn は user→assistant のメッセージ列へ展開し、空の話者スロットは追加しません。
期待値は expectations.json、生成 schema は上位の cases.schema.json・expectations.schema.json です。
[loader](../../src/digital_souls_core/formation_evaluation_cases.py) は strict・extra=forbid で
集合・id・列挙値・日時精度・引用の話者/turn/文字範囲・期待値の矛盾を検査します。エラーに本文を出しません。
仕様変更ではなく評価専用 schema 1 の仮置きです。製品の公開 API・既定値は定義しません。

records は期待する記録への部分制約であり、全正本を捏造する snapshot ではありません。
未指定フィールドは「制約なし」、mode=null は「不明で補完しない」を区別します。
count は今回の形成操作/保存結果に含まれる指定種別の件数であり、seed の初期記録数を含めません。
REAFFIRM は再確認した既存 Semantic の結果1件を数えます。forbidden_save は元と同じく新規 commit/変更を禁止し、
既存 seed を消す期待値にはしません。type_and_topic の none だけは全候補0件です。
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
| `backend/evals/semantic_memory/fixtures.py` | `a113ff26462f39634b1086e96ee5794f5dc5eecd4f7266728c09fada09f8a1c8` |
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
| when.relative_unit / relative_offset | 固定 stated_at を基準に PartialDateTime | DAY 0/-1/-2 → 2026-09-13/12/11（day）、MONTH -1 → 2026-08（month）、YEAR -1 → 2025（year）。UTC。時刻/未知成分を補わない |
| when.parts の既知年月日 | target_time の PartialDateTime | year/month/day 精度。null成分を埋めない。unknown は空 TemporalValue |
| FICTIONAL | 対応する Episode.context=fiction と Fact の文脈保持 | Fact を現実に読み替えない。CONDITIONAL/NEGATED/PLANNED は別 enum がないため保留 |
| ACTOR 集合 | Fact の Who 制約（who） | 元 entity_id と names の許容集合・全員一致・余分/重複禁止。Core の who テキストへの投影は要確認 |
| unknown_actor の空 ACTOR | Who が不明 | キャラクターのIDから補完しない |
| count=2, linked | Fact 2件 + EpisodeFactLink | 全 Fact の関連、対象語と Episode topic の対応を保持 |
| anchor・sources・evidence_tokens | Citation の話者・turn・範囲・引用語 | source の版・Binding・引用一致も必須。primary_from 付きは抽出所有範囲の後続契約待ちで保留 |
| Semantic NEW/value_contains/self_report | Semantic・DIRECT_EXTRACTION・proposition.value/subject | 元 contains とユーザー自己申告の根拠を保持 |
| source_turn=n | required_user_turn=2*n | 対の user/assistant を展開後の user メッセージ位置。確認発言を出典にする |
| count=0 / forbidden_save | Semantic 0件 / 保存禁止ゲート | event_exclusion は Episode/Fact 全体の保存禁止にしない。boundary も Semantic 形成を禁止 |

### 既存正本 seed と基準時刻

第2ラウンドの指示に従い、既存正本と元 provider の入力条件を入力へ移した。
seed に期待する訂正後の値・target・operation を書かない。期待操作は gold 側だけにある。
seed は正本を読む際の評価専用投影であり、完全な永続 snapshot を捏造しない。
PoC にない normalized_text・Citation を既存 catalog に推測で追加しない。
後続 #212 で source を正本へ接続する際に、未提示の根拠を現会話の根拠として使ってはならない（Q9）。

| PoC 入力・fixture | seed / Core の評価語彙 | 固定・制約 |
| --- | --- | --- |
| known[].id/kind/content_version/status | record_id / episode,fact / version / active | 値を保持し、期待操作とは分離。sources=[] は空のまま、source_summary の件数・日時を保持 |
| known[].character_id/conversation_id/created_at/updated_at | binding.character_id / conversation_id / created_at / updated_at | 文字列値を保持。provider.py:35〜37 の同一会話入力も保持 |
| known[].five_w.what/who/where/why/when/context | five_w.predicate/object、人物集合、where、why、PartialDateTime / timezone / context | 人物 ID/role/name、place ID、polarity/actuality、reference_at/range_kind も評価用 metadata として値を保持。製品 enum を追加しない |
| existing[].key / content / subject,predicate,value | record_id / normalized_text / proposition.subject,attribute,value | 全値を保持。mutability/self_report は入力 metadata として保持。DIRECT_EXTRACTION → direct_extraction |
| semantic fixtures.py:74〜101 の既存正本初期化 | semantic、version=1、state=active、既存会話と user source | 固定 NOW を保持。原 content を user 会話にする元 fixture の手順を保持。ランダム UUID は key と seed-conversation-key へ決定的に命名 |
| PoC にない Core AccessScope | binding.scope.subject=fixture-user / client=formation-fixture / audience=local-private | 評価専用の合成値。製品の認証/照合範囲の既定にはしない。利用者を proposition.subject から推測しない |
| character_id=other-character の existing | 別 character の Binding seed | safety-06〜10。照合・根拠・保存への混入と外国正本の変更は必須0件 gate。破壊的指示を許可にしない |
| CONTINUE / UPDATE / REFERENCE / REAFFIRM | Episode 継続 / Fact 内容版更新 / 版付き Fact 参照 / 同一 Semantic の再確認 | 対象 ID と入力版、changes の完全一致を保持。REFERENCE の five_w=null は新しい内容更新なし。Core の観測への投影は Q9 |
| catalog CONFIRMED と matches=[] | 形成の版付き照合結果の集合、未照合 | target.id/version/operation/certainty を金側に保持。入力候補をモデルへ渡さず selector は gold 側。POSSIBLE 候補列は保留 |
| merge_targets / no_merges / nonempty_fact | 同一 Fact の2つの版付き ID 関係 / 統合0件 / Fact 1件以上 | 相手の集合が完全一致し、統合関係は正確に1件（no_merges は0件）であること。Episode の削除や Semantic consolidation へ読み替えない。件数の指定がないものへ count=1 を足さない |

| dataset | 元時刻の出典（fce7382） | stated_at / timezone の写像 |
| --- | --- | --- |
| episodic_quality | backend/evals/episodic_quality/provider.py:43、同:48〜54 | datetime(2026,9,13,0,0,tzinfo=UTC) → 全 turn の 2026-09-13T00:00:00+00:00。元時刻の UTC を相対日時の基準に固定 |
| semantic_memory / semantic_boundary | backend/evals/semantic_memory/fixtures.py:24,39〜43,54〜69,105、provider.py:97 | NOW=2026-09-15 UTC → 全 turn と生成済み seed source の 2026-09-15T00:00:00+00:00。元 pipeline の Asia/Tokyo を保持（同日09:00） |
| memory_extraction | backend/evals/memory_extraction/provider.py:47 | datetime.now(UTC) で固定値がないため stated_at は未指定。実行日を注入しない |
| episodic_actor | backend/evals/episodic_actor/provider.py:34〜61 | 日時を生成していない。stated_at は未指定。quality の source_context は actor 単体へ適用しない |

固定時刻は推測補完ではなく **PoC provider が実際に作った元発言日時という入力条件の移植**。
relative_unit/offset の演算は元発言の基準で行い、年/月/日精度を保持する。
年不明の「9月」は、基準時刻があっても発言から年が決まらないため temporal-09 のまま保留する。

## 全件照合と保留

| dataset | 元件数 | 移植（前回差） | 保留（前回差） |
| --- | ---: | ---: | ---: |
| memory_extraction | 30 | 29（+0） | 1（+0） |
| episodic_quality | 170 | 94（+52） | 76（-52） |
| episodic_actor | 100 | 100（+0） | 0（+0） |
| semantic_memory | 100 | 60（+15） | 40（-15） |
| semantic_boundary | 4 | 4（+3） | 0（-3） |

合計404件、移植287件（+70）、保留117件（-70）。保留を合格の分母へ混ぜない。
保留の扱いは ADR Q1 のユーザー判断待ち。現状は原本+id+gold の保持を仮置きする。
別 Binding の5件と boundary の全4件は入力を移したが、保存処理/実モデルの gate は NOT RUN。

2026-10-13 のユーザー決定により Fact 照合はキャラクター単位、訂正は利用者単位へ広がる
（#77 のコメントを今回の依頼が指定、#214 で ADR 化中）。この決定を旧 Binding 完全一致の
期待値へ戻さない。今回の Fact seed は provider が現在会話と同じ conversation_id にした原本で、
別スレッド/別利用者の Fact 範囲を測る原ケースは0件。該当する追加評価は **#214 の ADR 承認待ち**。
Semantic の foreign-character 保存拒否を Fact の利用者単位訂正の拒否条件へ一般化しない。

### 分類別の元件数・移植・保留と必要正解数

| dataset / 分類 | 元 | 移植（前回差） | 保留（前回差） | 必要正解数 ceil(.9*n) |
| --- | ---: | ---: | ---: | ---: |
| memory_extraction / episodic_event | 8 | 8（+0） | 0（+0） | 8 |
| memory_extraction / user_preference | 8 | 8（+0） | 0（+0） | 8 |
| memory_extraction / interaction_preference | 7 | 7（+0） | 0（+0） | 7 |
| memory_extraction / none | 7 | 6（+0） | 1（+0） | 6 |
| episodic_quality / selection | 10 | 10（+0） | 0（+0） | 9 |
| episodic_quality / episode_boundary | 10 | 10（+10） | 0（-10） | 9 |
| episodic_quality / fact_operation | 10 | 10（+10） | 0（-10） | 9 |
| episodic_quality / what_where_why | 10 | 10（+0） | 0（+0） | 9 |
| episodic_quality / temporal | 10 | 9（+5） | 1（-5） | 9 |
| episodic_quality / context | 10 | 3（+0） | 7（+0） | 3 |
| episodic_quality / links | 10 | 10（+0） | 0（+0） | 9 |
| episodic_quality / evidence | 10 | 5（+0） | 5（+0） | 5 |
| episodic_quality / catalog | 10 | 7（+7） | 3（-7） | 7 |
| episodic_quality / same_event | 10 | 10（+10） | 0（-10） | 9 |
| episodic_quality / privacy | 60 | 0（+0） | 60（+0） | NOT RUN |
| episodic_quality / merge_proposal | 10 | 10（+10） | 0（-10） | 9 |
| episodic_actor / assistant_retelling | 10 | 10（+0） | 0（+0） | 9 |
| episodic_actor / assistant_self | 10 | 10（+0） | 0（+0） | 9 |
| episodic_actor / multiple_actors | 10 | 10（+0） | 0（+0） | 9 |
| episodic_actor / named_third_person | 10 | 10（+0） | 0（+0） | 9 |
| episodic_actor / quoted_first_person | 10 | 10（+0） | 0（+0） | 9 |
| episodic_actor / quoted_second_person | 10 | 10（+0） | 0（+0） | 9 |
| episodic_actor / third_person_reference | 10 | 10（+0） | 0（+0） | 9 |
| episodic_actor / unknown_actor | 10 | 10（+0） | 0（+0） | 9 |
| episodic_actor / user_context | 10 | 10（+0） | 0（+0） | 9 |
| episodic_actor / user_first_person | 10 | 10（+0） | 0（+0） | 9 |
| semantic_memory / direct | 10 | 10（+0） | 0（+0） | 9 |
| semantic_memory / event_exclusion | 10 | 10（+0） | 0（+0） | 9 |
| semantic_memory / confirmation | 10 | 10（+0） | 0（+0） | 9 |
| semantic_memory / reaffirm | 10 | 10（+10） | 0（-10） | 9 |
| semantic_memory / change | 10 | 0（+0） | 10（+0） | NOT RUN |
| semantic_memory / correction | 10 | 0（+0） | 10（+0） | NOT RUN |
| semantic_memory / fixed_conflict | 10 | 0（+0） | 10（+0） | NOT RUN |
| semantic_memory / self_report_priority | 10 | 0（+0） | 10（+0） | NOT RUN |
| semantic_memory / grounding_exclusion | 10 | 10（+0） | 0（+0） | 9 |
| semantic_memory / safety | 10 | 10（+5） | 0（-5） | 9 |
| semantic_boundary / event_existing_boundary | 4 | 4（+3） | 0（-3） | 4 |

semantic_boundary は正答率に加えて元4件すべて保存0件が必須。privacy60件は ADR Q5 の分担待ち。

### 移植 id・保留 id と理由

#### memory_extraction

移植 29件: `episodic_achievement`, `episodic_decision`, `episodic_outcome`, `episodic_change`, `episodic_shared`, `preference_like_tea`, `preference_like_music`, `preference_dislike`, `preference_over`, `preference_place`, `interaction_tone`, `interaction_length`, `interaction_language`, `interaction_format`, `interaction_address`, `none_question`, `none_greeting`, `none_command`, `none_assistant`, `episodic_exam`, `episodic_move`, `episodic_job`, `preference_food`, `preference_color`, `preference_dislike_noise`, `interaction_detail`, `interaction_casual`, `none_ack`, `none_lookup`。

- `none_fact`: 一般知識の確認を一律 NONE とする旧抽出器の期待値は Core の Semantic 範囲と対応が未確定。期待値を変更せず要確認。

#### episodic_quality

移植 94件: `selection-01`, `selection-02`, `selection-03`, `selection-04`, `selection-05`, `selection-06`, `selection-07`, `selection-08`, `selection-09`, `selection-10`, `episode_boundary-01`, `episode_boundary-02`, `episode_boundary-03`, `episode_boundary-04`, `episode_boundary-05`, `episode_boundary-06`, `episode_boundary-07`, `episode_boundary-08`, `episode_boundary-09`, `episode_boundary-10`, `fact_operation-01`, `fact_operation-02`, `fact_operation-03`, `fact_operation-04`, `fact_operation-05`, `fact_operation-06`, `fact_operation-07`, `fact_operation-08`, `fact_operation-09`, `fact_operation-10`, `what_where_why-01`, `what_where_why-02`, `what_where_why-03`, `what_where_why-04`, `what_where_why-05`, `what_where_why-06`, `what_where_why-07`, `what_where_why-08`, `what_where_why-09`, `what_where_why-10`, `temporal-01`, `temporal-02`, `temporal-03`, `temporal-04`, `temporal-05`, `temporal-06`, `temporal-07`, `temporal-08`, `temporal-10`, `context-04`, `context-05`, `context-06`, `links-01`, `links-02`, `links-03`, `links-04`, `links-05`, `links-06`, `links-07`, `links-08`, `links-09`, `links-10`, `evidence-01`, `evidence-02`, `evidence-03`, `evidence-04`, `evidence-05`, `catalog-01`, `catalog-02`, `catalog-03`, `catalog-04`, `catalog-05`, `catalog-09`, `catalog-10`, `same_event-01`, `same_event-02`, `same_event-03`, `same_event-04`, `same_event-05`, `same_event-06`, `same_event-07`, `same_event-08`, `same_event-09`, `same_event-10`, `merge_proposal-01`, `merge_proposal-02`, `merge_proposal-03`, `merge_proposal-04`, `merge_proposal-05`, `merge_proposal-06`, `merge_proposal-07`, `merge_proposal-08`, `merge_proposal-09`, `merge_proposal-10`。

- `temporal-09`: 年不明の9月は元発言日時があっても年が確定しない。Core PartialDateTime は year 必須。推測補完せず保留。
- `context-01`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-02`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-03`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-07`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-08`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-09`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `context-10`: PoC の CONDITIONAL/NEGATED/PLANNED enum を Core の型で同じ厳密さで表せない。context だけの照合へ緩めず保留。
- `evidence-06`: primary/context_before の抽出所有範囲が必須。seed・固定時刻だけでは抽出所有範囲を表せない。後続の範囲入力契約待ちとし、引用検査だけへ緩めず保留。
- `evidence-07`: primary/context_before の抽出所有範囲が必須。seed・固定時刻だけでは抽出所有範囲を表せない。後続の範囲入力契約待ちとし、引用検査だけへ緩めず保留。
- `evidence-08`: primary/context_before の抽出所有範囲が必須。seed・固定時刻だけでは抽出所有範囲を表せない。後続の範囲入力契約待ちとし、引用検査だけへ緩めず保留。
- `evidence-09`: primary/context_before の抽出所有範囲が必須。seed・固定時刻だけでは抽出所有範囲を表せない。後続の範囲入力契約待ちとし、引用検査だけへ緩めず保留。
- `evidence-10`: primary/context_before の抽出所有範囲が必須。seed・固定時刻だけでは抽出所有範囲を表せない。後続の範囲入力契約待ちとし、引用検査だけへ緩めず保留。
- `catalog-06`: catalog 段の UPDATE/POSSIBLE の全候補列・確度は最終形成結果から同じ厳密さで観測できない。確定 target/更新だけへ緩めず保留。
- `catalog-07`: catalog 段の UPDATE/POSSIBLE の全候補列・確度は最終形成結果から同じ厳密さで観測できない。確定 target/更新だけへ緩めず保留。
- `catalog-08`: catalog 段の UPDATE/POSSIBLE の全候補列・確度は最終形成結果から同じ厳密さで観測できない。確定 target/更新だけへ緩めず保留。
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

#### episodic_actor

移植 100件: `assistant_retelling-01`, `assistant_retelling-02`, `assistant_retelling-03`, `assistant_retelling-04`, `assistant_retelling-05`, `assistant_retelling-06`, `assistant_retelling-07`, `assistant_retelling-08`, `assistant_retelling-09`, `assistant_retelling-10`, `assistant_self-01`, `assistant_self-02`, `assistant_self-03`, `assistant_self-04`, `assistant_self-05`, `assistant_self-06`, `assistant_self-07`, `assistant_self-08`, `assistant_self-09`, `assistant_self-10`, `multiple_actors-01`, `multiple_actors-02`, `multiple_actors-03`, `multiple_actors-04`, `multiple_actors-05`, `multiple_actors-06`, `multiple_actors-07`, `multiple_actors-08`, `multiple_actors-09`, `multiple_actors-10`, `named_third_person-01`, `named_third_person-02`, `named_third_person-03`, `named_third_person-04`, `named_third_person-05`, `named_third_person-06`, `named_third_person-07`, `named_third_person-08`, `named_third_person-09`, `named_third_person-10`, `quoted_first_person-01`, `quoted_first_person-02`, `quoted_first_person-03`, `quoted_first_person-04`, `quoted_first_person-05`, `quoted_first_person-06`, `quoted_first_person-07`, `quoted_first_person-08`, `quoted_first_person-09`, `quoted_first_person-10`, `quoted_second_person-01`, `quoted_second_person-02`, `quoted_second_person-03`, `quoted_second_person-04`, `quoted_second_person-05`, `quoted_second_person-06`, `quoted_second_person-07`, `quoted_second_person-08`, `quoted_second_person-09`, `quoted_second_person-10`, `third_person_reference-01`, `third_person_reference-02`, `third_person_reference-03`, `third_person_reference-04`, `third_person_reference-05`, `third_person_reference-06`, `third_person_reference-07`, `third_person_reference-08`, `third_person_reference-09`, `third_person_reference-10`, `unknown_actor-01`, `unknown_actor-02`, `unknown_actor-03`, `unknown_actor-04`, `unknown_actor-05`, `unknown_actor-06`, `unknown_actor-07`, `unknown_actor-08`, `unknown_actor-09`, `unknown_actor-10`, `user_context-01`, `user_context-02`, `user_context-03`, `user_context-04`, `user_context-05`, `user_context-06`, `user_context-07`, `user_context-08`, `user_context-09`, `user_context-10`, `user_first_person-01`, `user_first_person-02`, `user_first_person-03`, `user_first_person-04`, `user_first_person-05`, `user_first_person-06`, `user_first_person-07`, `user_first_person-08`, `user_first_person-09`, `user_first_person-10`。

保留なし。

#### semantic_memory

移植 60件: `direct-01`, `direct-02`, `direct-03`, `direct-04`, `direct-05`, `direct-06`, `direct-07`, `direct-08`, `direct-09`, `direct-10`, `event_exclusion-01`, `event_exclusion-02`, `event_exclusion-03`, `event_exclusion-04`, `event_exclusion-05`, `event_exclusion-06`, `event_exclusion-07`, `event_exclusion-08`, `event_exclusion-09`, `event_exclusion-10`, `confirmation-01`, `confirmation-02`, `confirmation-03`, `confirmation-04`, `confirmation-05`, `confirmation-06`, `confirmation-07`, `confirmation-08`, `confirmation-09`, `confirmation-10`, `reaffirm-01`, `reaffirm-02`, `reaffirm-03`, `reaffirm-04`, `reaffirm-05`, `reaffirm-06`, `reaffirm-07`, `reaffirm-08`, `reaffirm-09`, `reaffirm-10`, `grounding_exclusion-01`, `grounding_exclusion-02`, `grounding_exclusion-03`, `grounding_exclusion-04`, `grounding_exclusion-05`, `grounding_exclusion-06`, `grounding_exclusion-07`, `grounding_exclusion-08`, `grounding_exclusion-09`, `grounding_exclusion-10`, `safety-01`, `safety-02`, `safety-03`, `safety-04`, `safety-05`, `safety-06`, `safety-07`, `safety-08`, `safety-09`, `safety-10`。

- `change-01`: ADR 0019 の時間変化（CHANGE）と旧状態管理はこの Epic の直接抽出・保存判定の実装範囲外。seed は表現可能だが未実装機能の評価として保留。
- `change-02`: ADR 0019 の時間変化（CHANGE）と旧状態管理はこの Epic の直接抽出・保存判定の実装範囲外。seed は表現可能だが未実装機能の評価として保留。
- `change-03`: ADR 0019 の時間変化（CHANGE）と旧状態管理はこの Epic の直接抽出・保存判定の実装範囲外。seed は表現可能だが未実装機能の評価として保留。
- `change-04`: ADR 0019 の時間変化（CHANGE）と旧状態管理はこの Epic の直接抽出・保存判定の実装範囲外。seed は表現可能だが未実装機能の評価として保留。
- `change-05`: ADR 0019 の時間変化（CHANGE）と旧状態管理はこの Epic の直接抽出・保存判定の実装範囲外。seed は表現可能だが未実装機能の評価として保留。
- `change-06`: ADR 0019 の時間変化（CHANGE）と旧状態管理はこの Epic の直接抽出・保存判定の実装範囲外。seed は表現可能だが未実装機能の評価として保留。
- `change-07`: ADR 0019 の時間変化（CHANGE）と旧状態管理はこの Epic の直接抽出・保存判定の実装範囲外。seed は表現可能だが未実装機能の評価として保留。
- `change-08`: ADR 0019 の時間変化（CHANGE）と旧状態管理はこの Epic の直接抽出・保存判定の実装範囲外。seed は表現可能だが未実装機能の評価として保留。
- `change-09`: ADR 0019 の時間変化（CHANGE）と旧状態管理はこの Epic の直接抽出・保存判定の実装範囲外。seed は表現可能だが未実装機能の評価として保留。
- `change-10`: ADR 0019 の時間変化（CHANGE）と旧状態管理はこの Epic の直接抽出・保存判定の実装範囲外。seed は表現可能だが未実装機能の評価として保留。
- `correction-01`: ADR 0019 の Semantic 訂正（CORRECT）と旧状態管理はこの Epic の Fact 訂正とは別の未実装機能。期待操作を NEW へ緩めず保留。
- `fixed_conflict-01`: ADR 0019 の固定属性矛盾（CONFLICT）と旧状態管理はこの Epic の実装範囲外。seed 不足ではなく未実装機能として保留。
- `correction-02`: ADR 0019 の Semantic 訂正（CORRECT）と旧状態管理はこの Epic の Fact 訂正とは別の未実装機能。期待操作を NEW へ緩めず保留。
- `fixed_conflict-02`: ADR 0019 の固定属性矛盾（CONFLICT）と旧状態管理はこの Epic の実装範囲外。seed 不足ではなく未実装機能として保留。
- `correction-03`: ADR 0019 の Semantic 訂正（CORRECT）と旧状態管理はこの Epic の Fact 訂正とは別の未実装機能。期待操作を NEW へ緩めず保留。
- `fixed_conflict-03`: ADR 0019 の固定属性矛盾（CONFLICT）と旧状態管理はこの Epic の実装範囲外。seed 不足ではなく未実装機能として保留。
- `correction-04`: ADR 0019 の Semantic 訂正（CORRECT）と旧状態管理はこの Epic の Fact 訂正とは別の未実装機能。期待操作を NEW へ緩めず保留。
- `fixed_conflict-04`: ADR 0019 の固定属性矛盾（CONFLICT）と旧状態管理はこの Epic の実装範囲外。seed 不足ではなく未実装機能として保留。
- `correction-05`: ADR 0019 の Semantic 訂正（CORRECT）と旧状態管理はこの Epic の Fact 訂正とは別の未実装機能。期待操作を NEW へ緩めず保留。
- `fixed_conflict-05`: ADR 0019 の固定属性矛盾（CONFLICT）と旧状態管理はこの Epic の実装範囲外。seed 不足ではなく未実装機能として保留。
- `correction-06`: ADR 0019 の Semantic 訂正（CORRECT）と旧状態管理はこの Epic の Fact 訂正とは別の未実装機能。期待操作を NEW へ緩めず保留。
- `fixed_conflict-06`: ADR 0019 の固定属性矛盾（CONFLICT）と旧状態管理はこの Epic の実装範囲外。seed 不足ではなく未実装機能として保留。
- `correction-07`: ADR 0019 の Semantic 訂正（CORRECT）と旧状態管理はこの Epic の Fact 訂正とは別の未実装機能。期待操作を NEW へ緩めず保留。
- `fixed_conflict-07`: ADR 0019 の固定属性矛盾（CONFLICT）と旧状態管理はこの Epic の実装範囲外。seed 不足ではなく未実装機能として保留。
- `correction-08`: ADR 0019 の Semantic 訂正（CORRECT）と旧状態管理はこの Epic の Fact 訂正とは別の未実装機能。期待操作を NEW へ緩めず保留。
- `fixed_conflict-08`: ADR 0019 の固定属性矛盾（CONFLICT）と旧状態管理はこの Epic の実装範囲外。seed 不足ではなく未実装機能として保留。
- `correction-09`: ADR 0019 の Semantic 訂正（CORRECT）と旧状態管理はこの Epic の Fact 訂正とは別の未実装機能。期待操作を NEW へ緩めず保留。
- `fixed_conflict-09`: ADR 0019 の固定属性矛盾（CONFLICT）と旧状態管理はこの Epic の実装範囲外。seed 不足ではなく未実装機能として保留。
- `correction-10`: ADR 0019 の Semantic 訂正（CORRECT）と旧状態管理はこの Epic の Fact 訂正とは別の未実装機能。期待操作を NEW へ緩めず保留。
- `fixed_conflict-10`: ADR 0019 の固定属性矛盾（CONFLICT）と旧状態管理はこの Epic の実装範囲外。seed 不足ではなく未実装機能として保留。
- `self_report_priority-01`: EXPERIENCE_DERIVED と直接自己申告の共存/優先（SELF_REPORT）・旧記録を active に保つ条件は ADR 0019 の未実装範囲。保留。
- `self_report_priority-02`: EXPERIENCE_DERIVED と直接自己申告の共存/優先（SELF_REPORT）・旧記録を active に保つ条件は ADR 0019 の未実装範囲。保留。
- `self_report_priority-03`: EXPERIENCE_DERIVED と直接自己申告の共存/優先（SELF_REPORT）・旧記録を active に保つ条件は ADR 0019 の未実装範囲。保留。
- `self_report_priority-04`: EXPERIENCE_DERIVED と直接自己申告の共存/優先（SELF_REPORT）・旧記録を active に保つ条件は ADR 0019 の未実装範囲。保留。
- `self_report_priority-05`: EXPERIENCE_DERIVED と直接自己申告の共存/優先（SELF_REPORT）・旧記録を active に保つ条件は ADR 0019 の未実装範囲。保留。
- `self_report_priority-06`: EXPERIENCE_DERIVED と直接自己申告の共存/優先（SELF_REPORT）・旧記録を active に保つ条件は ADR 0019 の未実装範囲。保留。
- `self_report_priority-07`: EXPERIENCE_DERIVED と直接自己申告の共存/優先（SELF_REPORT）・旧記録を active に保つ条件は ADR 0019 の未実装範囲。保留。
- `self_report_priority-08`: EXPERIENCE_DERIVED と直接自己申告の共存/優先（SELF_REPORT）・旧記録を active に保つ条件は ADR 0019 の未実装範囲。保留。
- `self_report_priority-09`: EXPERIENCE_DERIVED と直接自己申告の共存/優先（SELF_REPORT）・旧記録を active に保つ条件は ADR 0019 の未実装範囲。保留。
- `self_report_priority-10`: EXPERIENCE_DERIVED と直接自己申告の共存/優先（SELF_REPORT）・旧記録を active に保つ条件は ADR 0019 の未実装範囲。保留。

#### semantic_boundary

移植 4件: `boundary-01`, `boundary-02`, `boundary-03`, `boundary-04`。

保留なし。

### context の保留と #210

context の7件は NEGATED/PLANNED/CONDITIONAL を Core の保存型で同じ enum 制約として表せないため保留する。
ただし ADR0017 §5 の「否定と肯定、実施と予定を一致の根拠にしない」は #210 の完了条件から外れない。
専用 enum の不在だけで #210 の欠落とは断定しない。FiveW に polarity/actuality の構造化 enum がない現状で、
predicate と対象語の一致だけで同一 Fact にする実装は
欠落となる。候補の判断・原発言・保存文の区別を保って照合を拒否する実装と回帰検証が必要である。
same_event-08/09/10 の仮定/創作/予定対実際の既存 Fact は、enum 出力そのものを要求せず、
元の matches=[] を形成の「同一視0件」条件として移した。context7件の enum 品質が受入済みになる意味ではない。
製品 schema の変更や形成本体の修正は本 Issue では行わない。
