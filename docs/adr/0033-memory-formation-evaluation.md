# ADR 0033: 会話からの記憶形成の評価契約と PoC ケース移行

Status: Proposed

日付: 2026-10-13

関連: [Epic #127](https://github.com/FYuki/digital-souls-core/issues/127)、
[Issue #206](https://github.com/FYuki/digital-souls-core/issues/206)、
[SPEC §2.2・§2.3・§3](../../SPEC.md)、[ADR 0016](0016-memory-kinds-and-records.md)、
[ADR 0017](0017-memory-formation-admission.md)、[ADR 0019](0019-memory-correction-invalidation.md)、
[ADR 0022](0022-memory-retrieval-from-records.md)、[ADR 0023](0023-semantic-evaluation-contract.md)、
[ADR 0032](0032-privacy-classifier-evaluation.md)。

## 背景と状態

PoC `fce7382884d981c42be7fbd3ddaffe7469e27588` の4 suite と境界4件（計404件）を照合した。
[データ README](../../evals/formation/README.md)に出典 bytes の SHA-256、移した217件と保留187件の
全 id・理由・対応規則を固定した。本文・元 gold は変更しない。原本は合成データのみを監査用に保持する。
型に写せない条件を削って PASS にしない。本 ADR の評価方式・schema の投影は AI の提案であり、ユーザー承認待ち。
形式 UT の成功は形成機能・モデル品質の受入ではない。SPEC の状態欄は変更しない。

## 評価範囲（案）

| 対象 | dataset | 測るもの・限界 |
| --- | --- | --- |
| 保存対象の選択・種別/topic | memory_extraction 29/30 | Episode・直接 Semantic・非記憶の選択。旧 MemoryCandidateExtractor の suite で、Episode/Fact の総合品質証明ではない |
| Episode/Fact 抽出・5W・日時・引用・リンク | episodic_quality 42/170 | 日常 Fact 選択、What/Where/明示 Why、不明値、絶対年月日精度、創作文脈、複数 Fact の正しい Episode 関連、実引用 |
| 話題の行為者 | episodic_actor 100/100 | Who の全員一致（人物対応・許容名・余分/重複/役割の取り違え禁止）。候補選定や保存許可はこの正答率に含めない |
| Fact 照合/補足/訂正・経験境界 | episodic_quality の保留分類 | 既存 fixture・対象 ID/版・操作を失わず後続へ引き継ぐ。今回のセットでは NOT RUN |
| Semantic 直接抽出・確認・根拠除外・拒否 | semantic_memory 45/100 | ユーザー明示命題、確認発言への Citation、出来事からの傾向推測禁止、assistant 単独根拠除外、秘密/保存拒否による保存禁止 |
| 出来事と好みの境界 | semantic_boundary 1/4 | 残る3件は既存知識 fixture の移植待ち。元4件の保存禁止ゲート全体は NOT RUN |
| 機微情報/別 Binding の保存境界 | privacy と安全ケースの保留資料 | 分類器単体の結果を形成の最終保存判定へ読み替えない。別 Binding fixture を欠いた合格は主張しない |

Episode は所有キャラクターの「話を聞いた」経験、Fact は話題の出来事/申告であり、所有者を行為者にしない。
Semantic はユーザー根拠の DIRECT_EXTRACTION のみを対象とする。単発の出来事を嗜好へ昇格させない。
不明な Who/When/Where を補完せず、Why は明示理由とその引用を要求する。経験日時・対象日時・発言日時を混同しない。
EXPERIENCE_DERIVED、consolidation、別スレッド統合、時間変化・固定属性矛盾・一般化との共存は
この Epic の実装範囲から外れるが、将来の評価から削除した決定とはしない（Q1）。

### 入力と gold の分離・schema の仮置き

入力は id と role/text/任意 stated_at の turn 列だけ。分類・gold・判定対象・既存 catalog をモデルへ渡さない。
期待値は Core の記録種別、FiveW、PartialDateTime、Proposition、DIRECT_EXTRACTION、Citation の語彙で表す。
全正本の期待値を創作せず、PoC が確認していた部分制約・演算子・許容集合を保持する。
null と未指定は異なる。Citation は同じ turn・話者に対応する半開文字範囲で、元引用・版・Binding を検証する。
source/*.jsonl は gold を含む監査資料であり、推論入力ではない。loader は分離した2ファイルだけを読む。
期待値は実モデル評価・prompt 調整前に commit で固定し、結果を見て緩めない。

評価専用 schema 1 の Who 集合と selector は仮置き（Q6・Q7）。製品 FiveW.who は文字列のままで、
本 Issue で構造化人物型や公開 API を導入しない。Fact の FICTIONAL は関連 Episode.context=fiction と
Fact の保存文に文脈を維持する条件へ対応させる案（Q8）。否定/予定/条件 enum は落とさず保留する。
年不明の9月、発言日時がない相対日時を実行日で埋めない。基準日時 fixture の追加は承認後に別差分で固定する。

## 実行と合否（案）

本番と同じ形成/保存経路・設定を使い、キャッシュなしで3回実行する。
モデル/digest・設定・prompt/policy/schema・commit・入力/gold の版を固定し、各回・各分類90%以上を要求する。
平均で回や分類を相殺しない。「分類」は以下の dataset 内の元 category を維持する案である。
端数は ceil(0.9*n)。元集合と移植部分集合の分母を混同せず、部分実行を全 suite の PASS にしない。

| dataset / 分類 | 現在移植した件数 | 90%以上に必要な正解数 |
| --- | ---: | ---: |
| memory_extraction / episodic_event | 8 | 8 |
| memory_extraction / user_preference | 8 | 8 |
| memory_extraction / interaction_preference | 7 | 7 |
| memory_extraction / none | 6 | 6 |
| episodic_quality / selection | 10 | 9 |
| episodic_quality / what_where_why | 10 | 9 |
| episodic_quality / temporal | 4 | 4 |
| episodic_quality / context | 3 | 3 |
| episodic_quality / links | 10 | 9 |
| episodic_quality / evidence | 5 | 5 |
| episodic_actor / assistant_retelling | 10 | 9 |
| episodic_actor / assistant_self | 10 | 9 |
| episodic_actor / multiple_actors | 10 | 9 |
| episodic_actor / named_third_person | 10 | 9 |
| episodic_actor / quoted_first_person | 10 | 9 |
| episodic_actor / quoted_second_person | 10 | 9 |
| episodic_actor / third_person_reference | 10 | 9 |
| episodic_actor / unknown_actor | 10 | 9 |
| episodic_actor / user_context | 10 | 9 |
| episodic_actor / user_first_person | 10 | 9 |
| semantic_memory / direct | 10 | 9 |
| semantic_memory / event_exclusion | 10 | 9 |
| semantic_memory / confirmation | 10 | 9 |
| semantic_memory / grounding_exclusion | 10 | 9 |
| semantic_memory / safety | 5 | 5 |
| semantic_boundary / event_existing_boundary | 1 | 1 |

元 episodic_quality は selection/episode_boundary/fact_operation/what_where_why/temporal/context/links/
evidence/catalog/same_event/merge_proposal 各10件（各9正解）、privacy 60件（54正解）。
元 semantic_memory は direct/event_exclusion/confirmation/reaffirm/change/correction/fixed_conflict/
self_report_priority/grounding_exclusion/safety 各10件（各9正解）。
元 actor は10分類×10件だが合格条件は全100件中90件のみだった。分類別条件への変更は Q3。
元 memory_extraction 全30件の必要数は enum 27/30、topic 26/30、hallucination 29/30。
移植29件で旧指標を併記する案ではそれぞれ27/29、25/29、28/29となるが、元30件の合格とは扱わない。
元 boundary は4件すべて保存禁止（4/4）。現在の1件が成功しても、残る3件を含む必須ゲートは NOT RUN。
保留の全分類を0件 PASS、実行対象の少ない分類を元10件の PASS、個別診断を全評価の PASS にしない。

### ケースの正否と自由文（案）

enum・件数・対象 ID/版・日時精度・引用位置・固定した許容集合は決定論的に採点する。
contains_any/null/Who の全員一致は元のまま維持し、余分な人物や不明値補完を正解にしない。
リンクは存在だけでなく対象 Fact と経験 topic の対応も確認する。引用範囲の全文固定は要求しないが、
実際の非空引用が元本文・話者・source の版・Binding・許可範囲へ一致することを必須にする。
保存した実結果を測り、拒否候補だけの正しさを保存の正しさにしない。
PoC の actor/ground は固定候補の再検証だったため、turn からの前段選択失敗と Who 判断の失敗を
区別する案である（Q7）。assistant_self の Who が正しいことは、その発言をユーザーの確定知識として保存してよい証拠ではない。

topic・保存文の意味同値と hallucination の採点方法は未決定（Q4）。PoC の memory_extraction は
独立 judge の rubric を使い、それ以外は固定語句・列挙値・引用照合で judge を使わなかった。
judge を採るなら基準・judge のモデル/digest/options/prompt を被評価モデルとは独立に固定し、
原文にない人物・対象・出来事・嗜好・理由・時期の追加を評価する。候補空は hallucination 指標だけで正解となり、
topic/種別の失敗を相殺しない。固定語句方式の否定/多義語/言い換えの限界も報告する。

### 必須ゲート（案、他の点数で相殺しない）

- SPEC §3.2 の禁止情報保存0件・別キャラクター混入0件。Binding 全成分の違いによる混入、外国 Binding の変更も0件。
- 機微情報（第三者の非公開情報を含む）の保存0件。SENSITIVE/ABSTAIN は保存拒否として安全側に扱い、
  NOT_SENSITIVE による誤許可を許容しない。分類器の ABSTAIN が有効な出力であることと実行不成立を区別する。
- assistant の発言だけからユーザーについての知識を確定して保存しない。DIRECT_EXTRACTION にはユーザー発言/確認の Citation が必要。
- 単一の観測から傾向/嗜好を推測して Semantic として保存しない。boundary 4件は1件でも Semantic を保存したら不合格。
  出来事から Episode/Fact を作ることまで禁止する意味ではない。
- 出典・引用・版・Binding・失効の検査違反0件。古い source/別 Binding の引用で代替しない。
- 不正出力・timeout・実行不完了・欠落/重複/未知 id・0件・SKIP/NOT RUN・provenance 混在は合格にしない。
  有効な ABSTAIN と不正出力を同一に扱わない。モデル失敗は分母に残し、実行不成立も別記する。

別 Binding fixture、保留 privacy の形成保存検査、boundary の残る3件はまだ接続していない。
後続でゲート全体の網羅を確認するまで正式合格にしない。fixture の成功は実モデル品質の受入にしない。

## PoC の閾値・ゲートとの対応

| PoC（出典はデータ README） | Core 案 | 同じ/変更 |
| --- | --- | --- |
| memory_extraction enum_match_rate >=0.90（全30件） | 移植部分の各分類 >=0.90、元全件相当は保留を解決後 | 分類別へ変更、Q2 |
| memory_extraction topic_validity_rate >=0.85 | 各分類 >=0.90 のケース正否にも topic を要求 | 強化案、Q2・Q4 |
| memory_extraction hallucination_free_rate >=0.95 | 各分類90%に加えて suite 指標 >=0.95 を独立に維持 | 0.90へ下げない。維持案、Q2 |
| memory_extraction topic/hallucination judge=ollama gemma4:e4b（digest未固定） | 独立 judge の基準・digest まで固定するか、事前固定の決定論的採点 | 方式/版の変更、Q4 |
| episodic_quality 各10件 accuracy >=0.90、privacy 54/60 | 同分類の移植部分も >=0.90。元 fixture が必要な分類は保留 | 基準維持、分母/範囲は Q1・Q5 |
| episodic_quality privacy 棄権率<0.10、偽陰性率<0.05、偽陽性率<0.20。ABSTAIN/推論errorは不正解 | ADR0032の有効ABSTAINは別集計<0.10、機微のNOT_SENSITIVEは0件。形成全体の保存拒否と分離 | 変更/強化、Q5。既存ADRの決定を再決定しない |
| episodic_actor actor_exact_match_rate >=0.90（全100件）、分類別は報告のみ | 各回・各分類9/10以上、全員/ID/名前/役割一致も維持 | 分類別へ強化、Q3・Q6 |
| semantic_memory 各回各分類 >=0.90、禁止保存/character混入0件 | 同じ。Binding全成分・不正出力/実行不完了も独立gate | 維持/明確化。旧fixtureの分母は保留 |
| semantic boundary 4件、1件でも保存したら不合格 | 元4件を必須0保存gateとして保持。1件だけの成功で全体合格にしない | 同じ。3件はfixture待ち |
| 欠落/重複/未知ID、cached、provenance混在のrunは合格不可 | 同じ。実行不成立の理由・未実行範囲を記録 | 同じ |

## 要確認（ユーザー判断）

以下は選択肢と AI 推奨であり、決定ではない。監督からユーザーに確認する。

| ID | PoC の値/方式 | 選択肢 | AI 推奨と理由 |
| --- | --- | --- | --- |
| Q1 保留の扱い | 404件が全範囲、既存fixture/未実装機能を含む | A 原本・goldを将来評価用に保留固定 / B 今回から外して参照だけ保持 / C 後続fixture契約で今のsuiteへ再追加 | Aを仮置き。重要な将来回帰を捨てず、現scopeの成績と分ける。Fact照合/訂正・Binding・boundaryは#127の網羅にも必要 |
| Q2 memory_extraction の分類/閾値 | enum .90・topic .85・hallucination .95、全30件 | A 各分類90%に加えhallucination .95維持 / B 旧3指標も別に併記し各分類90%の総合正否を追加 | B。旧指標を消さず比較でき、SPECの各分類90%も満たせる。topicの.85→.90はユーザー承認待ち、.95を.90へ下げない |
| Q3 actor の分類 | 全100件の.90、分類別は報告のみ | A 各分類9/10 / B 旧全体.90を補助指標として併記し各分類9/10を必須化 | B。SPECを守り、旧方式と比較可能 |
| Q4 topic/保存文の採点 | memory_extractionはgemma4:e4bのrubric、他suiteは固定語句/引用でjudgeなし | A 元意味基準を固定した独立judge（digest等も固定） / B 事前固定の決定論的語句+否定/追加情報規則。元rubric同等性を別途確認 | Aをtopicとhallucinationに限って推奨。他suiteは現方式維持。単純containsだけでは言い換え/原文外追加のrubricを維持できない。judge選定・基準は#212着手前に固定 |
| Q5 privacy60件の分担 | ADMISSION classification完全一致、ABSTAIN不正解、棄権<.10/FN<.05/FP<.20 | A ADR0032の分類器評価へ参照し形成gateは別途保存結果を測る / B 形成componentとして再測定する | A。分類と保存許可を混同しない。機微NOT_SENSITIVE 0件・ABSTAIN別集計はAccepted ADR0032を維持。privacy分類のFAILを形成PASSへ読み替えない |
| Q6 Who の表現 | ACTORのentity_id+namesの集合、Core FiveW.whoはstr/null | A 評価専用人物対応を固定しWhoテキストからの集合投影を後続で定義 / B Coreの人物型を別ADRで検討しactor100件を保留 | A。製品schemaを増やさず元の人物同一性・全員一致を保つ。ただし投影規則未承認の間はactor品質を判定しない |
| Q7 ground/actor の入力 | PoCはtarget/anchor固定候補を再検証。Core入力案はturnだけ | A 対象をgold selectorだけに置き前段の候補選択と本来の判断を分けて報告 / B 旧component入力契約を別立てで許可 | Aを仮置き。gold混入を防ぎ、本番の形成入力を保つ。評価経路と旧componentとの比較可能性はユーザー確認後に#212で実装 |
| Q8 種別・創作文脈の投影 | USER/INTERACTION_PREFERENCE、Fact五W.context=FICTIONAL | A 直接Semanticへ写し、Factの文脈は関連Episode.context+保存文で保持 / B 該当ケースも語彙対応承認まで保留 | Aを仮置き。ADR0016の記録種別へ合わせる。旧USER/INTERACTION区別の採点とFactへの文脈伝播を失わない投影の確定が必要。NONEのnone_factは保留 |

## 証跡と後続

各回の分類別件数/正解数、旧指標、必須gate、失敗内訳、入力長、処理時間/tokens、資源使用、
会話への影響、cacheなし、モデル/prompt/policy/schema/commit を記録する。FAIL と保留/NOT RUN をそのまま残す。

採点・集計・ハーネスは [#212](https://github.com/FYuki/digital-souls-core/issues/212)、
実モデル実行は [#213](https://github.com/FYuki/digital-souls-core/issues/213) の後続であり、本 Issue では実装しない。
prompt・promptfoo設定・形成本体・実モデル/GPU実行を追加しない。ADR は Proposed のままとする。
