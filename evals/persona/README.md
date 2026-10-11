# 人格一貫性の評価ハーネス

[ADR 0029](../../docs/adr/0029-persona-consistency-evaluation.md) の決定論的な評価を実装します。
今回の CLI は外部通信・実 LLM・GPUを使わない fixture 専用です。成功した合成試験は
実モデルの人格品質の証拠ではなく、報告の `quality_evidence` は `false` です。

## 入力と採点

[ケース schema](cases.schema.json) は版1です。未知 field、空ケース集合、重複 id、欠けた分類、
空グループ、空白だけの表現、NFKC → casefold 後の重複表現（required/forbidden 間も含む）を拒否します。
入力は `id`、`category`、`messages`、`required`、`forbidden`、`card_fields` です。
分類は `self_intro`、`preferences`、`world_lore` の3つ。
ケースの会話は `user` / `assistant` とし、最後の message は `user` にします。
`card_fields` は期待値の根拠となる Card の field を注記します。gold の表現は provider に送りません。

```json
{
  "id": "intro",
  "category": "self_intro",
  "messages": [{"role": "user", "content": "自己紹介して。"}],
  "required": [["Luma"], ["わたし", "私"]],
  "forbidden": [{"text": "汎用アシスタント", "gate": "persona_break"}],
  "card_fields": ["data.description"]
}
```

`required` は各グループの any-of を全グループで満たす必要があります。`forbidden` の各表現に
`persona_break`、または `null`/省略（通常の禁止表現）を付けます。
応答と表現を NFKC → casefold し、部分文字列一致で採点します。空白や句読点は消しません。
否定文や引用内の表現も一致するため、意味や深い人格の一致を保証しません。

ケース集合の版は、default を含む検証済み schema の JSON（キーをsort、空白を除く、UTF-8）の
SHA-256です。本文の改変で変わり、JSON の空白・キー順の変更では変わりません。
期待値を評価結果に合わせて緩めません。実モデルを測る前には、確認済みのケースを commit で固定します。

## 入口と現在の制約

`persona_runtime.Entry` の登録辞書で入口を追加できます。必須の `chat` と `history` が
登録されていなければ実行を拒否します。未実装の活動入口は `NOT RUN（未実装の入口）` と報告し、
今回の合否には入れません。活動を登録した場合は、他の入口と同じ行列・採点で検証します。

Chat は `Inference.prepare` と provider を本番と同じ順で呼び、`Prepared.headers` の
`X-Character-Card-SHA256` を収集します。履歴は `Conversations.complete` を呼び、
保存の `Receipt.card_sha256` を収集します。fixture store は process 内のみで、過去の user/assistant を
seedし、今回の user を `TurnInput` へ渡します。ケース・回ごとにfreshな履歴を使い、retry receipt は返しません。
PostgreSQL 製品adapterの代わりに合成storeを使う IT1 であり、実保存の受入ではありません。

2026-10-11 ユーザー決定により、人格の上書きへの耐性（`override_resistance`）、
上書き指示に従った表現のゲート（`override_followed`）、会話履歴 API の system 拒否の構造検査・
ゲート（`override_channel_open`）をこのハーネスから外しました。
system を受け付ける入口だけへケースを適用する規則・対象外集計も撤去しています。
これらは [Issue #165](https://github.com/FYuki/digital-souls-core/issues/165) の
インジェクション対策の評価へ移します。製品 API の system 拒否仕様は維持します。
標準の `pass` fixture は、Chat・履歴の3分類すべてが各回100%、全体 **PASS**（exit 0）です。
ケース集合の各分類は1件以上必須で、空集合・0件の試験をPASSにしません。

## 実行と報告

```sh
export T=/home/asa/dev/digital-souls-evidence/history-stage1-tools
export PATH=$T/bin:$T/node/bin:$PATH
export TMPDIR=/dev/shm
export UV_OFFLINE=1
export LITELLM_LOCAL_MODEL_COST_MAP=True
uv run --no-sync python tools/evaluate-persona-consistency.py --runs 3 --output /dev/shm/persona-report.json
```

`--characters`、`--character-id`、`--profile` で設定を指定できます。
fixture CLI は `openai/fixture-only` / SDK / estimate の profile だけを受け付け、
実 transport や実 token counter は実行前に拒否します。[実モデル用 profile 例](local-profile.example.json) は
実行できません。実モデル・実 judge の adapter と実環境評価は NOT RUN です。
今回、新しい依存は追加していません。

各入口・分類・回（既定3回、cacheなし）の成功率が90%以上であることを要求します。
平均で不合格の分類・回を相殺しません。必須ゲートは `persona_break`、
`card_hash_mismatch`、`card_hash_missing`、`execution_incomplete` が0件です。Card の一致は同じ回の全応答を入口内・入口間とも照合します。
不一致ゲートの件数は違反した回の数で、該当回の case id、入口、hash を報告します。
本文採点のゲートは違反した応答の数です。欠落hash・未実行/実行エラーも追加の必須ゲートとしてFAILにします。
比較する全入口で実行済みの同じケース・回だけで入口間の合否不一致を計算します。
不一致は、`--mismatch-policy warn`（既定）なら警告、
`gate` なら全体の合否へ含めます。実品質評価でgateへ切り替えるときはユーザー決定と実行commitを証跡に残します。

報告 JSON は実行commitとdirty、ケース版、各回の率、ゲート件数・case id・場所、各応答のCard hash、
不一致の場所と扱い、Egogram を含みます。応答本文は既定では含めず、`--include-responses` の明示指定でのみ出します。
エラーには設定パス・ケース本文・provider例外を出しません。0件・欠落・重複・未知idの結果は拒否します。

judge interface はモデル・prompt・設定を識別する固定 id を持ちます。fake judge は
CP・NP・A・FC・AC を `fixture: unestimated` と入口ごとに並べます。これは数値推定ではありません。
実 judge は `NOT RUN`。judgeの結果を閾値と比較せず、合否・人格更新に使いません。

## 固定データ

- [合成Card](synthetic.card.json) / [設定](synthetic.characters.json)：架空の観測係 Luma。
- [合成ケース](synthetic.cases.json)：3分類各1件。好みのケースは過去1往復を含み、履歴の復元経路を検証。
- [応答fixture](responses.fixture.json)：`pass`、`fail`、`persona_break`、
  `mismatch`、`card_hash_mismatch`、`card_hash_missing`、`execution_incomplete` の7場面。`--scenario` で指定します。
  hash不一致のfixtureはChatで一部ケースのruntime snapshotを替え、本文と独立にゲートを検証します。
  hash欠落・実行不完了のfixtureは合成観測結果だけを変更し、各必須ゲートを検証します。
  90%境界でのwarn/gate差はUTの各分類10件の合成行列とIT1のCLIで検証します。
  標準CLIのPASS / exit 0と各違反のFAIL / exit 1を、CI対象のUT・IT1で固定しています。
- [固定 Miori ケース](miori.cases.json)：2026-10-11 ユーザー決定を反映した固定集合。
  自己紹介・話し方2件（自己紹介と挨拶）、好み・価値観2件、世界観・Lore3件（計7件）。
  名前と丁寧語、木漏れ日・黄昏、由来・光の断片・光のかけらを扱います。
  [同梱Card](../../characters/miori/miori.card.json) の field を各ケースに注記します。
  必須ゲート `persona_break` の表現は全ケース共通で「私は言語モデル」「AIアシスタントです」
  「大規模言語モデルとして」の3つに限定します。単独の「AI」「アシスタント」は禁止しません。
  固定ケースを `--cases` で指定した報告の `case_version` に集合のSHA-256、`cases_draft: false` を記録します。
  標準CLIはLumaの合成fixtureを使用するため、Mioriを実行する場合は対応するCard設定・応答fixtureも指定します。
  実モデルのMiori品質は未評価です。

## 今後の課題

運用開始後に、望ましい QA の組（質問と望ましい応答の例）を追加し、一致傾向の評価を加えます。
含んだら不合格の表現は最低限に保ちます。この追加評価は未実装です。

[今回の証跡](../../docs/evidence/2026-10-11-persona-consistency-harness.md) にコマンドと結果を記録します。
